"""IC Markets via MetaTrader 5.

Uses the official `MetaTrader5` Python package, which talks to a running MT5
terminal on the same Windows machine. All calls go through one worker thread.

Never-double-order rule: each order part gets a short deterministic tag
(derived from chat/message/index/part) that is stored in SQLite before sending
and written into the MT5 order comment. After an ambiguous result (no answer,
timeout, lost connection) we search positions, pending orders and deal history
for that tag and only resend if MT5 has never seen it.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from signal_copier import log
from signal_copier.executor.executor import ExecResult, client_id
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
ACCOUNT_TRADE_MODE_DEMO = 0


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
        logger.info("mt5_connected", login=acc.login, server=acc.server, demo=is_demo, currency=acc.currency)
        return {"login": acc.login, "server": acc.server, "demo": is_demo}

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
            )
        return self._info[instrument]

    async def pricing(self, instrument: str) -> tuple[Decimal, Decimal, Decimal]:
        """(bid, ask, account money per 1.0 price move per 1 lot)."""
        si = await self.symbol_info(instrument)
        tick = await self.call("symbol_info_tick", self.symbol(instrument))
        if tick is None:
            raise Mt5Error(f"no price for {self.symbol(instrument)}")
        tick_value = si.trade_tick_value_loss or si.trade_tick_value
        per_price_unit = Decimal(str(tick_value)) / Decimal(str(si.trade_tick_size))
        return Decimal(str(tick.bid)), Decimal(str(tick.ask)), per_price_unit


class Mt5Executor:
    def __init__(self, client: Mt5Client, storage: Storage, retry_attempts: int = 4,
                 retry_base_delay_s: float = 1.0, pending_expiry_minutes: int = 240,
                 tp_mode: str = "last"):
        self.client = client
        self.mt5 = client.mt5
        self.storage = storage
        self.retry_attempts = retry_attempts
        self.retry_base_delay_s = retry_base_delay_s
        self.pending_expiry = timedelta(minutes=pending_expiry_minutes)
        self.tp_mode = tp_mode

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
        res = await self.client.call("positions_get", **kw)
        return [p for p in (res or ()) if p.magic == self.client.cfg.magic]

    async def _find_by_tag(self, tag: str):
        """('position', obj) / ('order', obj) / ('deal', obj) / None. ('unknown', None) if MT5 can't be asked."""
        try:
            for p in await self._positions():
                if p.comment == tag:
                    return "position", p
            for o in await self.client.call("orders_get") or ():
                if o.comment == tag:
                    return "order", o
            now = datetime.now(timezone.utc)
            deals = await self.client.call("history_deals_get", now - timedelta(days=2), now + timedelta(hours=1))
            for d in deals or ():
                if d.comment == tag and d.entry == self.mt5.DEAL_ENTRY_IN:
                    return "deal", d
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
                if existing["status"] not in ("PENDING_SEND", "SENT"):
                    result.skipped.append(cid)
                    continue
                found = await self._find_by_tag(tag)
                if found is not None:
                    self._record_found(s, cid, found, lots, tp, result)
                    continue
            await self._send(s, cid, tag, lots, price, tp, info, si, result)
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
            tick = await self.client.call("symbol_info_tick", info.name)
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

    async def _send(self, s, cid, tag, lots, price, tp, info, si, result: ExecResult) -> None:
        for attempt in range(self.retry_attempts):
            req = await self._request(s, tag, lots, price, tp, info, si)  # fresh price on every attempt
            self.storage.update_order(cid, status="SENT")
            try:
                res = await self.client.call("order_send", req)
            except Exception as e:  # terminal/IPC problem: the order may or may not exist
                logger.warning("mt5_send_exception", client_id=cid, error=repr(e))
                res = None
            if res is not None and res.retcode in (DONE, DONE_PARTIAL, PLACED):
                await self._record_sent(s, cid, tag, res, lots, tp, result)
                return
            if res is not None and res.retcode in RETRY_FRESH_PRICE:
                logger.info("mt5_requote", client_id=cid, retcode=res.retcode, attempt=attempt + 1)
            elif res is None or res.retcode in AMBIGUOUS:
                found = await self._find_by_tag(tag)
                if found is not None:
                    self._record_found(s, cid, found, lots, tp, result)
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

    async def _record_sent(self, s, cid, tag, res, lots, tp, result: ExecResult) -> None:
        if res.retcode == PLACED:
            self.storage.update_order(cid, status="PENDING", oanda_order_id=str(res.order),
                                      response={"retcode": res.retcode, "order": res.order})
            result.pending.append({"order_id": str(res.order), "units": str(lots), "price": res.price, "tp": tp})
            return
        found = await self._find_by_tag(tag)
        if found and found[0] == "position":
            self._record_found(s, cid, found, lots, tp, result)
            return
        ticket = str(res.order)  # on hedging accounts the position ticket equals the opening order ticket
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
        elif kind == "deal":  # opened and already closed again
            self.storage.update_order(cid, status="FILLED", oanda_trade_id=str(obj.position_id))
            result.opened.append({"trade_id": str(obj.position_id), "units": str(obj.volume), "price": obj.price,
                                  "tp": tp})
        else:
            self.storage.update_order(cid, status="SENT")
            result.errors.append(f"{cid}: state unknown after an MT5 error, check the terminal")

    # ------------------------------------------------------------ follow-ups

    async def _close(self, pos, volume: float) -> None:
        m = self.mt5
        tick = await self.client.call("symbol_info_tick", pos.symbol)
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
                tick = await self.client.call("symbol_info_tick", pos.symbol)
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

    async def _live_positions(self, signal_key: str) -> list:
        out = []
        for t in self.storage.trades_for_signal(signal_key, open_only=True):
            found = await self.client.call("positions_get", ticket=int(t["trade_id"]))
            if found:
                out.append(found[0])
        return out

    async def apply_follow_up(self, fu: FollowUp, signal_key: str, info: InstrumentInfo) -> list[str]:
        done: list[str] = []
        positions = await self._live_positions(signal_key)
        step = Decimal(1).scaleb(-info.trade_units_precision)

        if fu.action is FollowUpAction.MOVE_SL_BE:
            for p in positions:
                await self._set_sltp(p, sl=p.price_open, tp=None)
                done.append(f"SL → BE {p.price_open} (#{p.ticket})")
        elif fu.action is FollowUpAction.MOVE_SL:
            for p in positions:
                await self._set_sltp(p, sl=self._px(fu.price, info), tp=None)
                done.append(f"SL → {fu.price} (#{p.ticket})")
        elif fu.action is FollowUpAction.CLOSE:
            for p in positions:
                await self._close(p, p.volume)
                done.append(f"затворена #{p.ticket} ({p.volume} лота)")
            done += await self._cancel_pending(signal_key)
        elif fu.action is FollowUpAction.CLOSE_PARTIAL:
            for p in positions:
                vol = (Decimal(str(p.volume)) * fu.fraction).quantize(step, rounding=ROUND_DOWN)
                if vol < info.minimum_trade_size:
                    continue
                if Decimal(str(p.volume)) - vol < info.minimum_trade_size:
                    vol = Decimal(str(p.volume))
                await self._close(p, float(vol))
                done.append(f"затворени {vol} от {p.volume} лота (#{p.ticket})")
        elif fu.action is FollowUpAction.CANCEL:
            done += await self._cancel_pending(signal_key)
        return done

    async def _cancel_pending(self, signal_key: str) -> list[str]:
        done = []
        for o in self.storage.orders_for_signal(signal_key):
            if o["status"] != "PENDING" or not o["oanda_order_id"]:
                continue
            res = await self.client.call("order_send", {"action": self.mt5.TRADE_ACTION_REMOVE,
                                                        "order": int(o["oanda_order_id"])})
            if res is not None and res.retcode == DONE:
                self.storage.update_order(o["client_id"], status="CANCELLED")
                done.append(f"отменена чакаща поръчка #{o['oanda_order_id']}")
            elif not await self.client.call("orders_get", ticket=int(o["oanda_order_id"])):
                self.storage.update_order(o["client_id"], status="CANCELLED")  # already gone (filled or expired)
            else:
                raise Mt5Error(f"cancel #{o['oanda_order_id']}: {getattr(res, 'retcode', None)}")
        return done

    async def amend_levels(self, signal_key: str, sl: Decimal | None, tps: list[Decimal],
                           info: InstrumentInfo) -> list[str]:
        done = []
        part_by_ticket = {o["oanda_trade_id"]: o["part"] for o in self.storage.orders_for_signal(signal_key)}
        for p in await self._live_positions(signal_key):
            part = part_by_ticket.get(str(p.ticket), 0)
            tp = tp_for_part(tps, part, self.tp_mode)
            await self._set_sltp(p, sl=self._px(sl, info) if sl is not None else None,
                                 tp=self._px(tp, info) if tp is not None else None)
            done.append(f"#{p.ticket}: SL {sl}, TP {tp}")
        return done

    # ------------------------------------------------------------ monitoring

    async def sync(self) -> tuple[list[dict], list[dict]]:
        opened, closed = [], []
        live = await self._positions()
        by_tag = {p.comment: p for p in live}
        live_tickets = {str(p.ticket) for p in live}

        for o in self.storage.orders_with_status("PENDING", "SENT"):
            p = by_tag.get(short_tag(o["client_id"]))
            if p is None:
                continue
            self.storage.update_order(o["client_id"], status="FILLED", oanda_trade_id=str(p.ticket))
            sign = -1 if Decimal(o["units"]) < 0 else 1
            self.storage.upsert_trade(str(p.ticket), o["signal_key"], o["client_id"], o["instrument"],
                                      Decimal(str(p.volume)) * sign, Decimal(str(p.price_open)),
                                      datetime.now(timezone.utc).isoformat())
            opened.append({"trade_id": str(p.ticket), "instrument": o["instrument"], "units": p.volume,
                           "price": p.price_open, "signal_key": o["signal_key"]})

        for t in self.storage.open_trades():
            if t["trade_id"] in live_tickets:
                continue
            deals = await self.client.call("history_deals_get", position=int(t["trade_id"])) or ()
            outs = [d for d in deals if d.entry != self.mt5.DEAL_ENTRY_IN]
            if not outs:
                continue  # history not synced yet; try again next round
            pl = sum(Decimal(str(d.profit)) + Decimal(str(d.commission)) + Decimal(str(d.swap))
                     + Decimal(str(getattr(d, "fee", 0))) for d in deals)
            close_price = Decimal(str(outs[-1].price))
            closed_at = datetime.fromtimestamp(outs[-1].time, timezone.utc).isoformat()
            self.storage.close_trade(t["trade_id"], close_price, pl, closed_at)
            closed.append({**t, "realized_pl": str(pl), "close_price": str(close_price)})
        return opened, closed
