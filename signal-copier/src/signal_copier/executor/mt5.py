"""IC Markets via MetaTrader 5.

Uses the official `MetaTrader5` Python package, which talks to a running MT5
terminal on the same Windows machine. All calls go through one worker thread.

Never-double-order rule: each order part gets a short deterministic tag
(derived from chat/message/index/part) that is stored in SQLite before sending
and written into the MT5 order comment. After an ambiguous result (no answer,
timeout, lost connection) we search positions, pending orders and deal history
for that tag for a while and never send it again: if MT5 does not show it, the
row stays SENT, the owner is told, and sync() adopts it if it turns up later.

The package never raises: a read that returns None is an IPC/terminal error and
is never taken for "nothing there" (Mt5Client.read raises Mt5Error instead).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace as NS
from typing import Any

from signal_copier import log
from signal_copier.executor.executor import ExecResult, Waiting, client_id
from signal_copier.models import EntryType, FollowUp, FollowUpAction, Side, Signal
from signal_copier.risk import InstrumentInfo, RiskDecision, plan_parts, tp_for_part
from signal_copier.settings import ConfigError, Mt5Cfg, Mt5Credentials
from signal_copier.storage import Storage

logger = log.get("mt5")

DONE, PLACED, DONE_PARTIAL = 10009, 10008, 10010
REQUOTE, PRICE_CHANGED, PRICE_OFF, TOO_MANY_REQUESTS = 10004, 10020, 10021, 10024
TIMEOUT, CONNECTION = 10012, 10031
RETRY_FRESH_PRICE = {REQUOTE, PRICE_CHANGED, PRICE_OFF, TOO_MANY_REQUESTS}
AMBIGUOUS = {TIMEOUT, CONNECTION}
# a market order (first send or retry after a requote) is not sent when the SL got this much further away than
# the distance the size was computed for: the same lots would risk more than planned
RETRY_MAX_SL_GROWTH = Decimal("1.1")
NOT_CONNECTED = "терминалът няма връзка със сървъра на брокера"
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
RES_S_OK = 1
# a command for a signal whose order is still without answer (SENT): what is done once the order shows up in MT5
LATER_FOR_UNKNOWN = {FollowUpAction.CLOSE: "ще я затворя", FollowUpAction.CANCEL: "ще я отменя",
                     FollowUpAction.MOVE_SL_BE: "ще преместя SL", FollowUpAction.MOVE_SL: "ще преместя SL"}


class Mt5Error(RuntimeError):
    pass


def short_tag(cid: str) -> str:
    """MT5 comments are limited to 31 chars, so the client id is hashed to a stable 14-char tag."""
    return "sc" + hashlib.sha1(cid.encode()).hexdigest()[:12]


def _units_precision(step: float) -> int:
    exp = Decimal(str(step)).normalize().as_tuple().exponent
    return max(0, -exp)


class Mt5Client:
    """Async facade over the blocking MetaTrader5 module (injectable for tests)."""

    def __init__(self, creds: Mt5Credentials, cfg: Mt5Cfg, mt5_module: Any = None):
        if mt5_module is None:
            try:
                import MetaTrader5 as mt5_module  # Windows only
            except ImportError as e:
                raise ConfigError("broker mt5 needs the MetaTrader5 package (Windows): pip install MetaTrader5") from e
        self.mt5 = mt5_module
        self.creds = creds
        self.cfg = cfg
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._info: dict[str, InstrumentInfo] = {}
        self.sleep = asyncio.sleep

    async def call(self, fn: str, *args, **kw):
        return await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: getattr(self.mt5, fn)(*args, **kw)
        )

    async def read(self, fn: str, *args, **kw):
        """A read whose None (IPC/terminal error) raises Mt5Error; () means MT5 answered "nothing".

        last_error() is asked in the same worker job, so no other call can overwrite it in between.
        """
        def job():
            res = getattr(self.mt5, fn)(*args, **kw)
            return res, (self.mt5.last_error() if res is None else None)

        res, err = await asyncio.get_running_loop().run_in_executor(self._pool, job)
        if res is not None:
            return res
        if err and err[0] == RES_S_OK:
            return ()
        raise Mt5Error(f"{fn} failed: {err}")

    def symbol(self, instrument: str) -> str:
        return self.cfg.symbol_map.get(instrument, instrument.replace("_", "")) + self.cfg.symbol_suffix

    async def connect(self) -> dict:
        kw: dict = dict(timeout=self.cfg.init_timeout_ms)
        if self.creds.login:
            kw.update(login=self.creds.login, password=self.creds.password, server=self.creds.server)
        if self.creds.path:
            kw["path"] = self.creds.path
        if not await self.call("initialize", **kw):
            raise Mt5Error(f"MT5 initialize failed: {await self.call('last_error')}")
        acc = await self.call("account_info")
        if acc is None:
            await self.call("shutdown")
            raise Mt5Error("MT5 terminal is not logged in to an account (log in in MT5 or set MT5_LOGIN/PASSWORD/SERVER)")
        is_demo = acc.trade_mode == ACCOUNT_TRADE_MODE_DEMO
        if self.creds.expect_demo is not None and is_demo != self.creds.expect_demo:
            await self.call("shutdown")
            want = "demo" if self.creds.expect_demo else "real"
            raise ConfigError(f"MT5 account {acc.login} is {'demo' if is_demo else 'real'}, but this mode needs {want}")
        if self.creds.expect_demo is not None:  # paper/live: refuse a terminal or account that cannot trade
            blockers = await self.trade_blockers()
            if blockers is None or blockers:
                why = "; ".join(blockers) if blockers else f"MT5 не отговаря ({await self.call('last_error')})"
                await self.call("shutdown")
                if blockers is None or blockers == [NOT_CONNECTED]:
                    # at boot the terminal answers before it has reached the broker: `run` starts again later
                    raise Mt5Error(f"MT5 още не може да търгува: {why}")
                raise ConfigError(f"MT5 не може да търгува: {why}")
        logger.info("mt5_connected", login=acc.login, server=acc.server, demo=is_demo, currency=acc.currency)
        return {"login": acc.login, "server": acc.server, "demo": is_demo}

    async def trade_blockers(self) -> list[str] | None:
        """Why the bot cannot trade on this terminal/account right now ([] = it can, None = MT5 did not answer)."""
        term = await self.call("terminal_info")
        acc = await self.call("account_info")
        if term is None or acc is None:
            return None
        out = []
        if not term.connected:
            out.append(NOT_CONNECTED)
        if not term.trade_allowed:
            out.append("бутонът Algo Trading в MT5 е изключен")
        if getattr(term, "tradeapi_disabled", False):
            out.append("търговията през Python API е забранена (Tools → Options → Expert Advisors)")
        if not acc.trade_allowed:
            out.append("акаунтът няма право да търгува (влязъл ли си с investor паролата?)")
        if not acc.trade_expert:
            out.append("брокерът е забранил автоматичната търговия за акаунта")
        if acc.margin_mode != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            out.append("акаунтът не е hedging (ботът разчита на отделна позиция за всеки сигнал)")
        return out

    async def aclose(self) -> None:
        try:
            await self.call("shutdown")
        finally:
            self._pool.shutdown(wait=False)

    # ----------------------------------------------------------------- reads

    async def account_summary(self) -> dict:
        a = await self.call("account_info")
        if a is None:
            raise Mt5Error(f"account_info failed: {await self.call('last_error')}")
        return {"balance": str(a.balance), "NAV": str(a.equity), "currency": a.currency}

    async def symbol_info(self, instrument: str):
        sym = self.symbol(instrument)
        await self.call("symbol_select", sym, True)
        info = await self.call("symbol_info", sym)
        if info is None:
            raise Mt5Error(f"symbol {sym} not found on this account (check mt5.symbol_map / symbol_suffix)")
        return info

    async def instrument(self, instrument: str) -> InstrumentInfo:
        if instrument not in self._info:
            si = await self.symbol_info(instrument)
            self._info[instrument] = InstrumentInfo(
                name=self.symbol(instrument),
                display_precision=int(si.digits),
                trade_units_precision=_units_precision(si.volume_step),  # units are lots here
                minimum_trade_size=Decimal(str(si.volume_min)),
                maximum_trade_size=Decimal(str(si.volume_max)) if getattr(si, "volume_max", 0) else None,
            )
        return self._info[instrument]

    async def pricing(self, instrument: str) -> tuple[Decimal, Decimal, Decimal]:
        """(bid, ask, account money per 1.0 price move per 1 lot)."""
        si = await self.symbol_info(instrument)
        tick = await self.call("symbol_info_tick", self.symbol(instrument))
        if tick is None:
            raise Mt5Error(f"no price for {self.symbol(instrument)} ({await self.call('last_error')})")
        tick_value = si.trade_tick_value_loss or si.trade_tick_value
        per_price_unit = Decimal(str(tick_value)) / Decimal(str(si.trade_tick_size))
        return Decimal(str(tick.bid)), Decimal(str(tick.ask)), per_price_unit


class _SyncReads:
    """The reads one sync() round needs for SENT rows, each done at most once. A failed read (None / IPC error)
    gives {} and sets `failed`: it can still not find anything, and it never proves that something is absent."""

    def __init__(self, ex: Mt5Executor):
        self.ex = ex
        self.failed = False
        self._orders: dict | None = None
        self._deals: dict | None = None

    async def orders(self) -> dict:
        if self._orders is None:
            try:
                self._orders = {o.comment: o for o in await self.ex.client.read("orders_get")}
            except Mt5Error as e:
                logger.warning("mt5_sync_orders_failed", error=str(e))
                self._orders, self.failed = {}, True
        return self._orders

    async def deals(self) -> dict:
        """Opening deals of the last days by tag (deal times are server time: the window reaches past UTC now)."""
        if self._deals is None:
            now = datetime.now(timezone.utc)
            try:
                deals = await self.ex.client.read("history_deals_get", now - timedelta(days=3), now + timedelta(days=1))
                self._deals = {d.comment: d for d in deals if d.entry == self.ex.mt5.DEAL_ENTRY_IN}
            except Mt5Error as e:
                logger.warning("mt5_sync_deals_failed", error=str(e))
                self._deals, self.failed = {}, True
        return self._deals


class Mt5Executor:
    def __init__(self, client: Mt5Client, storage: Storage, retry_attempts: int = 4,
                 retry_base_delay_s: float = 1.0, pending_expiry_minutes: int = 240,
                 tp_mode: str = "last", max_sl_deviation_pct: float = 10.0, unknown_expiry_minutes: int = 10):
        self.client = client
        self.mt5 = client.mt5
        self.storage = storage
        self.max_sl_deviation_pct = max_sl_deviation_pct  # an SL move further than this from entry is refused
        self.retry_attempts = retry_attempts
        self.retry_base_delay_s = retry_base_delay_s
        self.pending_expiry = timedelta(minutes=pending_expiry_minutes)
        self.tp_mode = tp_mode
        # an order whose answer was lost (SENT) and that MT5 shows nowhere for this long is taken as never executed
        self.unknown_expiry = timedelta(minutes=unknown_expiry_minutes)
        self._unseen: dict[str, datetime] = {}  # client id -> since when MT5 has shown no trace of it
        self.clock = lambda: datetime.now(timezone.utc)

    # ------------------------------------------------------------ helpers

    def _filling(self, si) -> int:
        mode = getattr(si, "filling_mode", 0)
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    @staticmethod
    def _px(value: Decimal | None, info: InstrumentInfo) -> float:
        return 0.0 if value is None else float(value.quantize(Decimal(1).scaleb(-info.display_precision)))

    async def _positions(self, **kw) -> list:
        """The bot's positions. Raises Mt5Error if MT5 can't be asked (never returns [] for that)."""
        res = await self.client.read("positions_get", **kw)
        return [p for p in res if p.magic == self.client.cfg.magic]

    async def _find_by_tag(self, tag: str):
        """('position', obj) / ('order', obj) / ('deal', obj) / None (MT5 answered and never saw it).

        ('unknown', None) if MT5 can't be asked: a None read is an IPC error, not "not found".
        """
        try:
            for p in await self._positions():
                if p.comment == tag:
                    return "position", p
            for o in await self.client.read("orders_get"):
                if o.comment == tag:
                    return "order", o
            # deal times are trade-server time (IC Markets UTC+2/+3): the window must reach well past UTC now
            now = datetime.now(timezone.utc)
            for d in await self.client.read("history_deals_get", now - timedelta(days=3), now + timedelta(days=1)):
                if d.comment == tag and d.entry == self.mt5.DEAL_ENTRY_IN:
                    live = await self.client.read("positions_get", ticket=d.position_id)
                    return ("position", live[0]) if live else ("deal", d)
        except Exception as e:
            logger.error("mt5_lookup_failed", tag=tag, error=repr(e))
            return "unknown", None
        return None

    async def _check(self, result, what: str) -> Any:
        if result is None:
            raise Mt5Error(f"{what}: no result ({await self.client.call('last_error')})")
        if result.retcode not in (DONE, PLACED, DONE_PARTIAL):
            raise Mt5Error(f"{what}: retcode {result.retcode} {result.comment}")
        return result

    # ------------------------------------------------------------ open

    async def open_signal(self, s: Signal, decision: RiskDecision, info: InstrumentInfo) -> ExecResult:
        result = ExecResult()
        parts = plan_parts(decision.units, list(s.tp), self.tp_mode, info.trade_units_precision,
                           info.minimum_trade_size)
        sign = Decimal(1) if s.side is Side.BUY else Decimal(-1)
        si = await self.client.symbol_info(s.symbol)

        for part, (lots, tp) in enumerate(parts):
            cid = client_id(s, part)
            tag = short_tag(cid)
            price = None if s.entry.type is EntryType.MARKET else s.entry.reference_price
            if not self.storage.create_order(cid, s.key, part, s.symbol, lots * sign, s.entry.type.value,
                                             price, s.sl, tp):
                existing = self.storage.get_order(cid)
                if existing["status"] in ("FILLED", "PENDING"):
                    result.skipped.append(cid)  # done earlier
                    continue
                if existing["status"] not in ("PENDING_SEND", "SENT"):  # rejected/failed/cancelled: nothing at MT5
                    result.errors.append(f"{cid}: {existing['status']} по-рано, не я пращам отново")
                    continue
                found = await self._find_by_tag(tag)
                if found is None and existing["status"] == "SENT":
                    found = ("unknown", None)  # it may have been sent before: never send it twice
                if found is not None:
                    self._record_found(s, cid, found, lots, tp, result)
                    continue
            await self._send(s, cid, tag, lots, price, tp, info, si, result,
                             decision.size_distance or decision.sl_distance)
        return result

    async def _request(self, s: Signal, tag: str, lots: Decimal, price: Decimal | None, tp: Decimal | None,
                       info: InstrumentInfo, si) -> dict:
        m = self.mt5
        buy = s.side is Side.BUY
        req = {
            "symbol": info.name,
            "volume": float(lots),
            "sl": self._px(s.sl, info),
            "tp": self._px(tp, info),
            "magic": self.client.cfg.magic,
            "comment": tag,
            "type_filling": self._filling(si),
        }
        if s.entry.type is EntryType.MARKET:
            tick = await self.client.read("symbol_info_tick", info.name)
            req.update(action=m.TRADE_ACTION_DEAL, type=m.ORDER_TYPE_BUY if buy else m.ORDER_TYPE_SELL,
                       price=tick.ask if buy else tick.bid, deviation=self.client.cfg.deviation_points,
                       type_time=m.ORDER_TIME_GTC)
        else:
            kind = {(EntryType.LIMIT, True): m.ORDER_TYPE_BUY_LIMIT, (EntryType.LIMIT, False): m.ORDER_TYPE_SELL_LIMIT,
                    (EntryType.STOP, True): m.ORDER_TYPE_BUY_STOP, (EntryType.STOP, False): m.ORDER_TYPE_SELL_STOP}
            req.update(action=m.TRADE_ACTION_PENDING, type=kind[(s.entry.type, buy)], price=self._px(price, info),
                       type_time=m.ORDER_TIME_SPECIFIED,
                       expiration=int(time.time() + self.pending_expiry.total_seconds()),
                       type_filling=m.ORDER_FILLING_RETURN)
        return req

    async def _send(self, s, cid, tag, lots, price, tp, info, si, result: ExecResult,
                    planned_sl_distance: Decimal | None = None) -> None:
        for attempt in range(self.retry_attempts):
            try:
                req = await self._request(s, tag, lots, price, tp, info, si)  # fresh price on every attempt
            except Mt5Error as e:  # nothing was sent in this attempt; earlier ones were refused (requotes)
                self.storage.update_order(cid, status="FAILED")
                result.errors.append(f"{cid}: not sent ({e})")
                return
            if planned_sl_distance and s.entry.type is EntryType.MARKET:
                # the lots were sized for the price the risk check saw: a retry (after a requote / PRICE_OFF)
                # must not take a price where the same lots risk clearly more
                now_distance = abs(Decimal(str(req["price"])) - s.sl)
                if now_distance > planned_sl_distance * RETRY_MAX_SL_GROWTH:
                    self.storage.update_order(cid, status="FAILED")
                    result.errors.append(f"{cid}: не я пращам отново: цената се отмести и SL е на {now_distance} "
                                         f"вместо {planned_sl_distance}, рискът би бил по-голям от планирания")
                    return
            self.storage.update_order(cid, status="SENT")
            try:
                res = await self.client.call("order_send", req)
            except Exception as e:  # terminal/IPC problem: the order may or may not exist
                logger.warning("mt5_send_exception", client_id=cid, error=repr(e))
                res = None
            if res is not None and res.retcode in (DONE, DONE_PARTIAL, PLACED):
                await self._record_sent(s, cid, tag, res, lots, tp, result)
                return
            if res is not None and res.retcode in RETRY_FRESH_PRICE:  # refused by the server: safe to send again
                logger.info("mt5_requote", client_id=cid, retcode=res.retcode, attempt=attempt + 1)
            elif res is None or res.retcode in AMBIGUOUS:
                # the server may still execute it (a TIMEOUT can fill a moment later): look, never resend
                logger.warning("mt5_send_ambiguous", client_id=cid, retcode=getattr(res, "retcode", None),
                               error=await self.client.call("last_error") if res is None else None)
                self._record_found(s, cid, await self._await_tag(tag), lots, tp, result)
                return
            else:
                self.storage.update_order(cid, status="REJECTED", response={"retcode": res.retcode,
                                                                            "comment": res.comment})
                result.errors.append(f"{cid}: rejected {res.retcode} {res.comment}")
                return
            if attempt < self.retry_attempts - 1:
                await self.client.sleep(self.retry_base_delay_s * 2**attempt)
        found = await self._find_by_tag(tag)
        if found is not None:
            self._record_found(s, cid, found, lots, tp, result)
            return
        self.storage.update_order(cid, status="FAILED")
        result.errors.append(f"{cid}: not sent after {self.retry_attempts} attempts")

    async def _await_tag(self, tag: str):
        """Poll for an order whose result was lost (about 15 s with the defaults). ('unknown', None) if unseen."""
        for attempt in range(self.retry_attempts + 1):
            found = await self._find_by_tag(tag)
            if found is not None and found[0] != "unknown":
                return found
            if attempt < self.retry_attempts:
                await self.client.sleep(self.retry_base_delay_s * 2**attempt)
        return "unknown", None

    async def _record_sent(self, s, cid, tag, res, lots, tp, result: ExecResult) -> None:
        if res.retcode == PLACED:
            self.storage.update_order(cid, status="PENDING", oanda_order_id=str(res.order),
                                      response={"retcode": res.retcode, "order": res.order})
            result.pending.append({"order_id": str(res.order), "units": str(lots), "price": res.price, "tp": tp})
            return
        try:  # on hedging accounts the position ticket equals the opening order ticket
            found = await self.client.read("positions_get", ticket=res.order)
        except Mt5Error:
            found = ()
        if found:
            self._record_found(s, cid, ("position", found[0]), lots, tp, result)
            return
        ticket = str(res.order)
        self.storage.update_order(cid, status="FILLED", oanda_order_id=str(res.order), oanda_trade_id=ticket,
                                  response={"retcode": res.retcode, "deal": res.deal, "order": res.order})
        self.storage.upsert_trade(ticket, s.key, cid, s.symbol, Decimal(str(res.volume)) * self._sign(s),
                                  Decimal(str(res.price)), datetime.now(timezone.utc).isoformat())
        result.opened.append({"trade_id": ticket, "units": str(res.volume), "price": res.price, "tp": tp})

    @staticmethod
    def _sign(s: Signal) -> Decimal:
        return Decimal(1) if s.side is Side.BUY else Decimal(-1)

    def _record_found(self, s, cid, found, lots, tp, result: ExecResult) -> None:
        kind, obj = found
        if kind == "position":
            ticket = str(obj.ticket)
            self.storage.update_order(cid, status="FILLED", oanda_trade_id=ticket)
            self.storage.upsert_trade(ticket, s.key, cid, s.symbol, Decimal(str(obj.volume)) * self._sign(s),
                                      Decimal(str(obj.price_open)), datetime.now(timezone.utc).isoformat())
            result.opened.append({"trade_id": ticket, "units": str(obj.volume), "price": obj.price_open, "tp": tp})
        elif kind == "order":
            self.storage.update_order(cid, status="PENDING", oanda_order_id=str(obj.ticket))
            result.pending.append({"order_id": str(obj.ticket), "units": str(lots), "price": obj.price_open,
                                   "tp": tp})
        elif kind == "deal":  # opened and already closed again: sync() records the close and its P/L
            ticket = str(obj.position_id)
            self.storage.update_order(cid, status="FILLED", oanda_trade_id=ticket)
            self.storage.upsert_trade(ticket, s.key, cid, s.symbol, Decimal(str(obj.volume)) * self._sign(s),
                                      Decimal(str(obj.price)), datetime.now(timezone.utc).isoformat())
            result.opened.append({"trade_id": ticket, "units": str(obj.volume), "price": obj.price, "tp": tp})
        else:
            self.storage.update_order(cid, status="SENT")
            result.unknown.append(cid)
            result.errors.append(f"{cid}: MT5 не потвърди поръчката и не я виждам. НЕ я пращам повторно: "
                                 "провери терминала. Ако се появи там, ботът ще я поеме сам.")

    def _adopt(self, o: dict, p) -> None:
        """Order row `o` turned out to be live position `p` in MT5: record the trade, the signal is live."""
        sign = -1 if Decimal(o["units"]) < 0 else 1
        self.storage.update_order(o["client_id"], status="FILLED", oanda_trade_id=str(p.ticket))
        self.storage.upsert_trade(str(p.ticket), o["signal_key"], o["client_id"], o["instrument"],
                                  Decimal(str(p.volume)) * sign, Decimal(str(p.price_open)),
                                  datetime.now(timezone.utc).isoformat())
        self._signal_live(o["signal_key"])

    def _signal_live(self, signal_key: str) -> None:
        sig = self.storage.get_signal(signal_key)
        if sig is not None and sig["status"] != "EXECUTED":  # e.g. FAILED after an unknown send
            self.storage.set_signal_status(signal_key, "EXECUTED", "found_in_mt5")

    # ------------------------------------------------------------ follow-ups

    async def _close(self, pos, volume: float) -> None:
        m = self.mt5
        tick = await self.client.read("symbol_info_tick", pos.symbol)
        buy_pos = pos.type == m.POSITION_TYPE_BUY
        si = await self.client.call("symbol_info", pos.symbol)
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": pos.symbol, "position": pos.ticket, "volume": volume,
               "type": m.ORDER_TYPE_SELL if buy_pos else m.ORDER_TYPE_BUY,
               "price": tick.bid if buy_pos else tick.ask, "deviation": self.client.cfg.deviation_points,
               "magic": self.client.cfg.magic, "comment": "sc close", "type_time": m.ORDER_TIME_GTC,
               "type_filling": self._filling(si)}
        for attempt in range(self.retry_attempts):
            res = await self.client.call("order_send", req)
            if res is not None and res.retcode in RETRY_FRESH_PRICE:
                tick = await self.client.read("symbol_info_tick", pos.symbol)
                req["price"] = tick.bid if buy_pos else tick.ask
                continue
            await self._check(res, f"close {pos.ticket}")
            return
        raise Mt5Error(f"close {pos.ticket}: requotes")

    async def _set_sltp(self, pos, sl: float | None, tp: float | None) -> None:
        req = {"action": self.mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket,
               "sl": pos.sl if sl is None else sl, "tp": pos.tp if tp is None else tp,
               "magic": self.client.cfg.magic}
        await self._check(await self.client.call("order_send", req), f"SL/TP {pos.ticket}")

    def _looser(self, pos, sl: float) -> bool:
        """True if `sl` is further from the price than the position's SL (more money at risk)."""
        if not pos.sl:
            return False
        return sl < pos.sl if pos.type == self.mt5.POSITION_TYPE_BUY else sl > pos.sl

    async def _move_sl(self, pos, sl: float, what: str = "") -> str:
        """Tighten the SL to `sl` (BE or a price). Never loosens it; waits while the price is on the wrong side."""
        target = f"{what} {sl}".strip()
        if pos.sl == sl:
            return f"SL вече е {target} (#{pos.ticket})"
        if self._looser(pos, sl):
            return (f"⚠️ SL остава {pos.sl} (#{pos.ticket}): {target} е по-далеч от цената, не разхлабвам SL "
                    "автоматично")
        if abs(sl - pos.price_open) / pos.price_open * 100 > self.max_sl_deviation_pct:
            return f"⚠️ SL остава {pos.sl} (#{pos.ticket}): {sl} е твърде далеч от входа {pos.price_open}, не го прилагам"
        buy = pos.type == self.mt5.POSITION_TYPE_BUY
        tick = await self.client.read("symbol_info_tick", pos.symbol)
        si = await self.client.call("symbol_info", pos.symbol)
        gap = (getattr(si, "trade_stops_level", 0) or 0) * (getattr(si, "point", 0) or 0)
        price = tick.bid if buy else tick.ask
        if (buy and sl >= price - gap) or (not buy and sl <= price + gap):
            # MT5 refuses an SL beyond the price (10016): keep the current SL and try again from the monitor
            return Waiting(f"⏳ SL → {target} (#{pos.ticket}): цената е {price}, още не може; ще го преместя, щом "
                           f"цената го позволи. Дотогава SL остава {pos.sl}")
        await self._set_sltp(pos, sl=sl, tp=None)
        return f"SL → {target} (#{pos.ticket})"

    async def _live_positions(self, signal_key: str) -> list:
        out = []
        for t in self.storage.trades_for_signal(signal_key, open_only=True):
            found = await self.client.read("positions_get", ticket=int(t["trade_id"]))  # None raises: not "gone"
            if found:
                out.append(found[0])
        return out

    async def _reconcile_signal(self, signal_key: str) -> list[str]:
        """Before acting on a follow-up: adopt what the signal's PENDING/SENT orders became since the last sync."""
        done = []
        for o in self.storage.orders_for_signal(signal_key):
            if o["status"] == "PENDING" and o["oanda_order_id"]:
                # hedging: a filled pending order keeps its ticket as the position ticket
                pos = await self.client.read("positions_get", ticket=int(o["oanda_order_id"]))
                if pos:
                    self._adopt(o, pos[0])
                    done.append(f"чакащата поръчка #{o['oanda_order_id']} вече е изпълнена ({pos[0].volume} лота)")
            elif o["status"] == "SENT":
                found = await self._find_by_tag(short_tag(o["client_id"]))
                if found is None:
                    continue  # MT5 answered and has nothing with this tag
                kind, obj = found
                if kind == "unknown":
                    raise Mt5Error(f"{o['client_id']}: MT5 не отговаря, не знам дали поръчката съществува")
                if kind == "order":
                    self.storage.update_order(o["client_id"], status="PENDING", oanda_order_id=str(obj.ticket))
                    self._signal_live(signal_key)
                elif kind == "position":
                    self._adopt(o, obj)
                else:  # filled and closed again: sync() records the close
                    self._adopt(o, NS(ticket=obj.position_id, volume=obj.volume, price_open=obj.price))
        return done

    async def apply_follow_up(self, fu: FollowUp, signal_key: str, info: InstrumentInfo) -> list[str]:
        done = await self._reconcile_signal(signal_key)
        positions = await self._live_positions(signal_key)
        step = Decimal(1).scaleb(-info.trade_units_precision)

        if fu.action is FollowUpAction.MOVE_SL_BE:
            for p in positions:
                done.append(await self._move_sl(p, p.price_open, "BE"))
        elif fu.action is FollowUpAction.MOVE_SL:
            for p in positions:
                done.append(await self._move_sl(p, self._px(fu.price, info)))
        elif fu.action is FollowUpAction.CLOSE:
            for p in positions:
                await self._close(p, p.volume)
                done.append(f"затворена #{p.ticket} ({p.volume} лота)")
            cancelled, filled = await self._cancel_pending(signal_key)
            done += cancelled
            for p in filled:  # filled while we tried to remove it
                await self._close(p, p.volume)
                done.append(f"затворена #{p.ticket} ({p.volume} лота)")
        elif fu.action is FollowUpAction.CLOSE_PARTIAL:
            for p in positions:
                vol = (Decimal(str(p.volume)) * fu.fraction).quantize(step, rounding=ROUND_DOWN)
                if vol < info.minimum_trade_size:
                    done.append(f"пропусната #{p.ticket}: {p.volume} лота е твърде малко за частично затваряне")
                    continue
                if Decimal(str(p.volume)) - vol < info.minimum_trade_size:
                    vol = Decimal(str(p.volume))
                await self._close(p, float(vol))
                done.append(f"затворени {vol} от {p.volume} лота (#{p.ticket})")
        elif fu.action is FollowUpAction.CANCEL:
            cancelled, filled = await self._cancel_pending(signal_key)
            done += cancelled
            for p in positions + filled:  # never close on a "cancel": say plainly that it is open
                done.append(f"⚠️ поръчката вече е изпълнена: позиция #{p.ticket} ({p.volume} лота) остава отворена "
                            "със SL/TP, не я затварям автоматично")
        unseen = [o["client_id"] for o in self.storage.orders_for_signal(signal_key) if o["status"] == "SENT"]
        if unseen and fu.action in LATER_FOR_UNKNOWN:
            # it may still turn up (filled late): the monitor runs the command again until it does or is given up
            done.append(Waiting(f"⏳ поръчката без отговор ({', '.join(unseen)}) още не се вижда в MT5: "
                                f"{LATER_FOR_UNKNOWN[fu.action]}, щом се появи"))
        return done

    async def _cancel_pending(self, signal_key: str) -> tuple[list[str], list]:
        """Remove the signal's pending orders. Returns (what was done, positions they turned into meanwhile)."""
        m = self.mt5
        done, filled = [], []
        for o in self.storage.orders_for_signal(signal_key):
            if o["status"] != "PENDING" or not o["oanda_order_id"]:
                continue
            ticket = int(o["oanda_order_id"])
            res = await self.client.call("order_send", {"action": m.TRADE_ACTION_REMOVE, "order": ticket})
            if res is not None and res.retcode == DONE:
                self.storage.update_order(o["client_id"], status="CANCELLED")
                done.append(f"отменена чакаща поръчка #{ticket}")
                continue
            # no answer or refused: find out where the order is before touching the DB (None reads raise)
            what = f"cancel #{ticket}: {getattr(res, 'retcode', None)} {getattr(res, 'comment', '')}".strip()
            if await self.client.read("orders_get", ticket=ticket):
                raise Mt5Error(f"{what}: поръчката още е активна в MT5")
            pos = await self.client.read("positions_get", ticket=ticket)  # filled: the position keeps the ticket
            if pos:
                self._adopt(o, pos[0])
                filled.append(pos[0])
                continue
            hist = await self.client.read("history_orders_get", ticket=ticket)
            state = hist[0].state if hist else None
            if state in (m.ORDER_STATE_CANCELED, m.ORDER_STATE_EXPIRED, m.ORDER_STATE_REJECTED):
                self.storage.update_order(o["client_id"], status="CANCELLED")
                done.append(f"чакаща поръчка #{ticket} вече не е активна (отменена/изтекла)")
            elif state == m.ORDER_STATE_FILLED:  # filled and closed again: sync() records the close
                deal = next((d for d in await self.client.read("history_deals_get", position=ticket)
                             if d.entry == m.DEAL_ENTRY_IN), None)
                if deal is None:
                    raise Mt5Error(f"{what}: изпълнена, но сделката още не е в историята")
                self._adopt(o, NS(ticket=ticket, volume=deal.volume, price_open=deal.price))
                done.append(f"чакаща поръчка #{ticket} е била изпълнена и вече затворена")
            else:
                raise Mt5Error(f"{what}: не знам какво е станало с поръчката, провери терминала")
        return done, filled

    async def amend_levels(self, signal_key: str, sl: Decimal | None, tps: list[Decimal],
                           info: InstrumentInfo) -> list[str]:
        """The channel edited SL/TP. sl=None: the SL was not edited, keep the live one (it may be at BE).

        Only what differs from the live position is sent, and a wider SL is never applied automatically.
        """
        done = await self._reconcile_signal(signal_key)
        part_by_ticket = {o["oanda_trade_id"]: o["part"] for o in self.storage.orders_for_signal(signal_key)}
        for p in await self._live_positions(signal_key):
            part = part_by_ticket.get(str(p.ticket), 0)
            tp = tp_for_part(tps, part, self.tp_mode)
            new_tp = self._px(tp, info) if tp is not None else None
            new_tp = None if new_tp == p.tp else new_tp
            new_sl = self._px(sl, info) if sl is not None else None
            if new_sl is not None and (new_sl == p.sl or self._looser(p, new_sl)):
                if new_sl != p.sl:
                    done.append(f"⚠️ #{p.ticket}: SL остава {p.sl}, не го разхлабвам до {new_sl} (рискът би станал "
                                "по-голям от планирания). Ако го искаш, смени го ръчно в MT5.")
                new_sl = None
            if new_sl is None and new_tp is None:
                continue
            await self._set_sltp(p, sl=new_sl, tp=new_tp)
            done.append(f"#{p.ticket}: SL {p.sl if new_sl is None else new_sl}, TP {p.tp if new_tp is None else new_tp}")
        return done

    async def cancel_all_pending(self) -> tuple[list[str], list[str]]:
        """Daily stop: remove every pending order of the bot. Returns (what was done, errors)."""
        done, errors = [], []
        for key in dict.fromkeys(o["signal_key"] for o in self.storage.orders_with_status("PENDING")):
            try:
                cancelled, filled = await self._cancel_pending(key)
            except Exception as e:  # e.g. MT5 not answering: the row stays PENDING, the next round tries again
                errors.append(f"{key}: {e}")
                continue
            done += cancelled
            done += [f"⚠️ поръчката вече е изпълнена: позиция #{p.ticket} ({p.volume} лота) остава отворена със SL/TP"
                     for p in filled]
        return done, errors

    # ------------------------------------------------------------ monitoring

    async def sync(self) -> tuple[list[dict], list[dict]]:
        """Returns (changes, closed). Changes carry kind: filled | recovered | recovered_closed | pending |
        expired (an order without answer that MT5 never showed: marked FAILED, see _expire_unseen)."""
        opened, closed = [], []
        try:
            live = await self._positions()
        except Mt5Error as e:  # MT5 can't be asked: this round proves nothing about what is open
            logger.warning("mt5_sync_skipped", error=str(e))
            self._unseen.clear()
            return opened, closed
        by_tag = {p.comment: p for p in live}
        by_ticket = {str(p.ticket): p for p in live}
        reads = _SyncReads(self)
        now = self.clock()
        since = (now - timedelta(days=3)).isoformat()
        unseen = []

        # an order given up as never executed that shows up after all is adopted like any other
        rows = self.storage.orders_with_status("PENDING", "SENT") + self.storage.orders_expired_unknown(since)
        for o in rows:
            tag = short_tag(o["client_id"])
            p = by_tag.get(tag) or by_ticket.get(o["oanda_order_id"] or "")
            if p is not None:
                self._unseen.pop(o["client_id"], None)
                self._adopt(o, p)
                opened.append({"trade_id": str(p.ticket), "instrument": o["instrument"], "units": p.volume,
                               "price": p.price_open, "signal_key": o["signal_key"],
                               "kind": "filled" if o["status"] == "PENDING" else "recovered"})
                continue
            if o["status"] == "PENDING":
                if o["oanda_order_id"] and tag not in await reads.orders():
                    # no longer working: filled and closed again between two rounds (or while the bot was down)?
                    # Its position keeps the order's ticket; the loop below then records the close and its P/L
                    d = await self._opening_deal(int(o["oanda_order_id"]))
                    if d is not None:
                        self._adopt(o, NS(ticket=d.position_id, volume=d.volume, price_open=d.price))
                        opened.append({"trade_id": str(d.position_id), "instrument": o["instrument"],
                                       "units": d.volume, "price": d.price, "signal_key": o["signal_key"],
                                       "kind": "filled"})
                continue
            # a SENT row whose answer was lost may be a working pending order, or filled and closed already
            po = (await reads.orders()).get(tag)
            if po is not None:
                self._unseen.pop(o["client_id"], None)
                self.storage.update_order(o["client_id"], status="PENDING", oanda_order_id=str(po.ticket))
                self._signal_live(o["signal_key"])
                opened.append({"trade_id": str(po.ticket), "instrument": o["instrument"], "units": po.volume_current,
                               "price": po.price_open, "signal_key": o["signal_key"], "kind": "pending"})
                continue
            d = (await reads.deals()).get(tag)
            if d is not None:  # its position is not live (it would be in by_tag): opened and closed again
                self._unseen.pop(o["client_id"], None)
                self._adopt(o, NS(ticket=d.position_id, volume=d.volume, price_open=d.price))
                opened.append({"trade_id": str(d.position_id), "instrument": o["instrument"], "units": d.volume,
                               "price": d.price, "signal_key": o["signal_key"], "kind": "recovered_closed"})
                continue
            if o["status"] == "SENT":
                unseen.append(o)
        opened += await self._expire_unseen(unseen, reads, now)

        for t in self.storage.open_trades():
            if t["trade_id"] in by_ticket:
                continue
            try:
                deals = await self.client.read("history_deals_get", position=int(t["trade_id"]))
            except Mt5Error:
                continue  # try again next round
            outs = [d for d in deals if d.entry != self.mt5.DEAL_ENTRY_IN]
            in_vol = sum(Decimal(str(d.volume)) for d in deals if d.entry == self.mt5.DEAL_ENTRY_IN)
            out_vol = sum(Decimal(str(d.volume)) for d in outs)
            if not outs or out_vol < (in_vol or abs(Decimal(t["units"]))):
                continue  # history not synced yet, or only partly closed: still open
            pl = sum(Decimal(str(d.profit)) + Decimal(str(d.commission)) + Decimal(str(d.swap))
                     + Decimal(str(getattr(d, "fee", 0))) for d in deals)
            close_price = Decimal(str(outs[-1].price))
            closed_at = datetime.fromtimestamp(outs[-1].time, timezone.utc).isoformat()
            self.storage.close_trade(t["trade_id"], close_price, pl, closed_at)
            closed.append({**t, "realized_pl": str(pl), "close_price": str(close_price)})
        return opened, closed

    async def _opening_deal(self, ticket: int):
        """The deal that opened position `ticket`, None if there is none or MT5 does not answer (tried next round)."""
        try:
            deals = await self.client.read("history_deals_get", position=ticket)
        except Mt5Error as e:
            logger.warning("mt5_sync_deals_failed", ticket=ticket, error=str(e))
            return None
        return next((d for d in deals if d.entry == self.mt5.DEAL_ENTRY_IN), None)

    async def _expire_unseen(self, rows: list[dict], reads: _SyncReads, now: datetime) -> list[dict]:
        """SENT rows (answer lost) that MT5 shows nowhere: not a position, not a pending order, no opening deal.

        Only when every read of the round answered and the terminal is connected to the broker does a round count
        as "no trace"; after `unknown_expiry` of such rounds in a row the order is taken as never executed: FAILED,
        its max_open / per-symbol slot is free and the owner is told. A failed read or a disconnected terminal
        (whose lists may be stale) starts the wait again, so it can never expire an order by itself.
        """
        ids = {o["client_id"] for o in rows}
        self._unseen = {cid: t for cid, t in self._unseen.items() if cid in ids}
        if not rows:
            return []
        term = await self.client.call("terminal_info")
        if reads.failed or term is None or not term.connected:
            self._unseen.clear()
            return []
        out = []
        for o in rows:
            first = self._unseen.setdefault(o["client_id"], now)
            if now - first < self.unknown_expiry:
                continue
            del self._unseen[o["client_id"]]
            self.storage.update_order(o["client_id"], status="FAILED",
                                      response={"expired_unknown": now.isoformat(), "unseen_since": first.isoformat()})
            self._signal_failed_if_nothing_left(o["signal_key"])
            logger.warning("mt5_unknown_order_expired", client_id=o["client_id"], unseen_since=first.isoformat())
            out.append({"trade_id": o["client_id"], "instrument": o["instrument"], "units": abs(Decimal(o["units"])),
                        "price": o["price"], "signal_key": o["signal_key"], "kind": "expired",
                        "minutes": int((now - first).total_seconds() // 60)})
        return out

    def _signal_failed_if_nothing_left(self, signal_key: str) -> None:
        """No order of the signal ever reached the market: it is not live (follow-ups, edits, slots)."""
        orders = self.storage.orders_for_signal(signal_key)
        if any(o["status"] in ("PENDING_SEND", "SENT", "PENDING", "FILLED") for o in orders):
            return
        sig = self.storage.get_signal(signal_key)
        if sig is not None and sig["status"] == "EXECUTED":
            self.storage.set_signal_status(signal_key, "FAILED", "unknown_order_expired")
