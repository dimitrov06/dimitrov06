"""Turns approved signals and follow-up commands into OANDA orders.

Never-double-order rule: every order part has a deterministic client id
(chat/message/index/part) that is stored BEFORE sending and sent to OANDA as
clientExtensions.id. After a network error we ask OANDA for that client id
and only resend if OANDA has never seen it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from signal_copier import log
from signal_copier.executor.oanda import OandaClient, OandaError, TransientError
from signal_copier.models import EntryType, FollowUp, FollowUpAction, Side, Signal
from signal_copier.risk import InstrumentInfo, RiskDecision, split_units
from signal_copier.storage import Storage

logger = log.get("executor")


def fmt_price(value: Decimal, info: InstrumentInfo) -> str:
    return str(value.quantize(Decimal(1).scaleb(-info.display_precision)))


def fmt_units(units: Decimal, info: InstrumentInfo) -> str:
    return str(units.quantize(Decimal(1).scaleb(-info.trade_units_precision), rounding=ROUND_DOWN))


def client_id(s: Signal, part: int) -> str:
    return f"sc_{str(s.chat_id).replace('-', 'n')}_{s.message_id}_{s.index}_{part}"


@dataclass
class ExecResult:
    opened: list[dict] = field(default_factory=list)  # {"trade_id", "units", "price", "tp"}
    pending: list[dict] = field(default_factory=list)  # {"order_id", "units", "price", "tp"}
    skipped: list[str] = field(default_factory=list)  # already sent earlier
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.opened or self.pending) and not self.errors


class Executor:
    def __init__(self, client: OandaClient, storage: Storage, retry_attempts: int = 4,
                 retry_base_delay_s: float = 1.0, pending_expiry_minutes: int = 240):
        self.client = client
        self.storage = storage
        self.retry_attempts = retry_attempts
        self.retry_base_delay_s = retry_base_delay_s
        self.pending_expiry = timedelta(minutes=pending_expiry_minutes)

    # ------------------------------------------------------------ open trades

    async def open_signal(self, s: Signal, decision: RiskDecision, info: InstrumentInfo) -> ExecResult:
        result = ExecResult()
        tps: list[Decimal | None] = list(s.tp) or [None]
        sizes = split_units(decision.units, len(tps), info.trade_units_precision, info.minimum_trade_size)
        sign = Decimal(1) if s.side is Side.BUY else Decimal(-1)

        for part, (units, tp) in enumerate(zip(sizes, tps)):
            cid = client_id(s, part)
            price = None if s.entry.type is EntryType.MARKET else s.entry.reference_price
            created = self.storage.create_order(cid, s.key, part, s.symbol, units * sign, s.entry.type.value,
                                                price, s.sl, tp)
            if not created:
                existing = self.storage.get_order(cid)
                if existing["status"] in ("PENDING_SEND", "SENT"):
                    # crashed or lost the response last time: ask OANDA before sending again
                    found = await self._lookup(cid)
                    if found is not None:
                        self._record_lookup(s, cid, found, units * sign, tp, result)
                        continue
                else:
                    result.skipped.append(cid)  # done, rejected or failed earlier: never resend automatically
                    continue
            order = self._order_body(s, cid, units * sign, price, tp, info)
            await self._send(s, cid, order, units * sign, tp, result)
        return result

    def _order_body(self, s: Signal, cid: str, units: Decimal, price: Decimal | None, tp: Decimal | None,
                    info: InstrumentInfo) -> dict:
        ext = {"id": cid, "tag": "signal-copier", "comment": f"{s.symbol} {s.side.value} msg {s.message_id}"}
        order: dict = {
            "instrument": s.symbol,
            "units": fmt_units(units, info),
            "positionFill": "DEFAULT",
            "clientExtensions": ext,
            "tradeClientExtensions": ext,
            "stopLossOnFill": {"price": fmt_price(s.sl, info), "timeInForce": "GTC"},
        }
        if tp is not None:
            order["takeProfitOnFill"] = {"price": fmt_price(tp, info), "timeInForce": "GTC"}
        if s.entry.type is EntryType.MARKET:
            order.update(type="MARKET", timeInForce="FOK")
        else:
            expiry = datetime.now(timezone.utc) + self.pending_expiry
            order.update(
                type=s.entry.type.value,
                price=fmt_price(price, info),
                timeInForce="GTD",
                gtdTime=expiry.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                triggerCondition="DEFAULT",
            )
        return order

    async def _send(self, s: Signal, cid: str, order: dict, units: Decimal, tp: Decimal | None,
                    result: ExecResult) -> None:
        for attempt in range(self.retry_attempts):
            try:
                body = await self.client.create_order_once(order)
                self._record_response(s, cid, body, units, tp, result)
                return
            except OandaError as e:
                if e.error_code == "CLIENT_ORDER_ID_ALREADY_EXISTS":
                    break  # sent before (e.g. response lost) -> look it up below
                self.storage.update_order(cid, status="REJECTED", response=e.body)
                result.errors.append(f"{cid}: rejected {e.error_code or e.status}")
                return
            except TransientError as e:
                logger.warning("order_send_transient", client_id=cid, attempt=attempt + 1, error=str(e))
                self.storage.update_order(cid, status="SENT")
                found = await self._lookup(cid)
                if found is not None:
                    self._record_lookup(s, cid, found, units, tp, result)
                    return
                if attempt < self.retry_attempts - 1:
                    await self.client.sleep(self.retry_base_delay_s * 2**attempt)
        found = await self._lookup(cid)
        if found is not None:
            self._record_lookup(s, cid, found, units, tp, result)
            return
        self.storage.update_order(cid, status="FAILED")
        result.errors.append(f"{cid}: not sent after {self.retry_attempts} attempts")

    async def _lookup(self, cid: str) -> dict | None:
        try:
            return await self.client.order_by_client_id(cid)
        except (TransientError, OandaError) as e:
            logger.error("order_lookup_failed", client_id=cid, error=str(e))
            # Unknown state: pretend it exists so we never send it twice; the monitor reconciles it.
            return {"state": "UNKNOWN"}

    def _record_response(self, s, cid, body: dict, units, tp, result: ExecResult) -> None:
        fill = body.get("orderFillTransaction")
        cancel = body.get("orderCancelTransaction")
        create = body.get("orderCreateTransaction", {})
        if fill and fill.get("tradeOpened"):
            t = fill["tradeOpened"]
            self.storage.update_order(cid, status="FILLED", oanda_order_id=create.get("id"),
                                      oanda_trade_id=t["tradeID"], response=body)
            self.storage.upsert_trade(t["tradeID"], s.key, cid, s.symbol, Decimal(t["units"]),
                                      Decimal(t.get("price") or fill["price"]), fill.get("time", ""))
            result.opened.append({"trade_id": t["tradeID"], "units": t["units"], "price": fill.get("price"), "tp": tp})
        elif cancel:
            self.storage.update_order(cid, status="CANCELLED", oanda_order_id=create.get("id"), response=body)
            result.errors.append(f"{cid}: cancelled by OANDA ({cancel.get('reason')})")
        else:
            self.storage.update_order(cid, status="PENDING", oanda_order_id=create.get("id"), response=body)
            result.pending.append({"order_id": create.get("id"), "units": str(units), "price": order_price(body),
                                   "tp": tp})

    def _record_lookup(self, s, cid, order: dict, units, tp, result: ExecResult) -> None:
        state = order.get("state")
        if state == "FILLED" and order.get("tradeOpenedID"):
            self.storage.update_order(cid, status="FILLED", oanda_order_id=order.get("id"),
                                      oanda_trade_id=order["tradeOpenedID"], response=order)
            result.opened.append({"trade_id": order["tradeOpenedID"], "units": str(units), "price": None, "tp": tp})
            # trade row (with open price) is filled in by the monitor
        elif state == "PENDING":
            self.storage.update_order(cid, status="PENDING", oanda_order_id=order.get("id"), response=order)
            result.pending.append({"order_id": order.get("id"), "units": str(units), "price": order.get("price"),
                                   "tp": tp})
        elif state == "UNKNOWN":
            self.storage.update_order(cid, status="SENT")
            result.errors.append(f"{cid}: state unknown after network error, check OANDA")
        else:
            self.storage.update_order(cid, status="CANCELLED", oanda_order_id=order.get("id"), response=order)
            result.errors.append(f"{cid}: order {state}")

    # ---------------------------------------------------------------- follow-ups

    async def apply_follow_up(self, fu: FollowUp, signal_key: str, info: InstrumentInfo) -> list[str]:
        """Returns a list of human-readable actions taken."""
        done: list[str] = []
        trades = self.storage.trades_for_signal(signal_key, open_only=True)
        pending = [o for o in self.storage.orders_for_signal(signal_key) if o["status"] == "PENDING"]

        if fu.action is FollowUpAction.MOVE_SL_BE:
            for t in trades:
                await self.client.set_trade_orders(t["trade_id"], sl=fmt_price(Decimal(t["open_price"]), info))
                done.append(f"SL → BE {t['open_price']} (trade {t['trade_id']})")
        elif fu.action is FollowUpAction.MOVE_SL:
            for t in trades:
                await self.client.set_trade_orders(t["trade_id"], sl=fmt_price(fu.price, info))
                done.append(f"SL → {fu.price} (trade {t['trade_id']})")
        elif fu.action is FollowUpAction.CLOSE:
            for t in trades:
                await self.client.close_trade(t["trade_id"])
                done.append(f"closed trade {t['trade_id']}")
            done += await self._cancel_pending(pending)
        elif fu.action is FollowUpAction.CLOSE_PARTIAL:
            for t in trades:
                live = await self.client.get_trade(t["trade_id"])
                current = abs(Decimal(live["currentUnits"]))
                units = (current * fu.fraction).quantize(Decimal(1).scaleb(-info.trade_units_precision),
                                                        rounding=ROUND_DOWN)
                if units <= 0:
                    continue
                if units >= current:
                    await self.client.close_trade(t["trade_id"])
                else:
                    await self.client.close_trade(t["trade_id"], fmt_units(units, info))
                done.append(f"closed {units} of {current} (trade {t['trade_id']})")
        elif fu.action is FollowUpAction.CANCEL:
            done += await self._cancel_pending(pending)
        # TP_HIT / SL_HIT are informational: OANDA already handled them via attached orders
        return done

    async def _cancel_pending(self, pending: list[dict]) -> list[str]:
        done = []
        for o in pending:
            try:
                await self.client.cancel_order(f"@{o['client_id']}")
            except OandaError as e:
                if e.status != 404:
                    raise
            self.storage.update_order(o["client_id"], status="CANCELLED")
            done.append(f"cancelled pending order {o['client_id']}")
        return done

    async def amend_levels(self, signal_key: str, sl: Decimal | None, tps: list[Decimal],
                           info: InstrumentInfo) -> list[str]:
        """The channel edited SL/TP of a signal we already traded: move SL/TP of the open trades."""
        done = []
        orders = {o["oanda_trade_id"]: o for o in self.storage.orders_for_signal(signal_key) if o["oanda_trade_id"]}
        for t in self.storage.trades_for_signal(signal_key, open_only=True):
            part = orders.get(t["trade_id"], {}).get("part", 0)
            tp = tps[part] if part < len(tps) else None
            await self.client.set_trade_orders(
                t["trade_id"],
                sl=fmt_price(sl, info) if sl is not None else None,
                tp=fmt_price(tp, info) if tp is not None else None,
            )
            done.append(f"trade {t['trade_id']}: SL {sl}, TP {tp}")
        return done

    # ------------------------------------------------------------- monitoring

    async def sync(self) -> tuple[list[dict], list[dict]]:
        """Reconcile with OANDA. Returns (newly_opened, newly_closed) trades."""
        opened, closed = [], []
        live = await self.client.open_trades()
        by_client = {(t.get("clientExtensions") or {}).get("id"): t for t in live}
        live_ids = {t["id"] for t in live}

        for o in self.storage.orders_with_status("PENDING", "SENT"):
            t = by_client.get(o["client_id"])
            if t is None:
                continue
            self.storage.update_order(o["client_id"], status="FILLED", oanda_trade_id=t["id"])
            self.storage.upsert_trade(t["id"], o["signal_key"], o["client_id"], t["instrument"],
                                      Decimal(t["initialUnits"]), Decimal(t["price"]), t.get("openTime", ""))
            opened.append({"trade_id": t["id"], "instrument": t["instrument"], "units": t["initialUnits"],
                           "price": t["price"], "signal_key": o["signal_key"]})

        for o in self.storage.orders_with_status("FILLED"):
            if o["oanda_trade_id"] and o["oanda_trade_id"] in live_ids:
                t = next(x for x in live if x["id"] == o["oanda_trade_id"])
                self.storage.upsert_trade(t["id"], o["signal_key"], o["client_id"], t["instrument"],
                                          Decimal(t["initialUnits"]), Decimal(t["price"]), t.get("openTime", ""))

        for t in self.storage.open_trades():
            if t["trade_id"] in live_ids:
                continue
            info = await self.client.get_trade(t["trade_id"])
            if info.get("state") != "CLOSED":
                continue
            pl = Decimal(info.get("realizedPL", "0"))
            close_price = info.get("averageClosePrice")
            self.storage.close_trade(t["trade_id"], Decimal(close_price) if close_price else None, pl,
                                     info.get("closeTime"))
            closed.append({**t, "realized_pl": str(pl), "close_price": close_price})
        return opened, closed


def order_price(body: dict) -> str | None:
    return (body.get("orderCreateTransaction") or {}).get("price")
