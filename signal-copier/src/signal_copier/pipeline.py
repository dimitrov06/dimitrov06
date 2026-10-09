"""message -> parse -> (record | risk -> execute) -> store + notify.

Kept free of Telethon so it can be tested end-to-end with a fake OANDA.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from signal_copier import log
from signal_copier.executor import Executor, OandaClient
from signal_copier.models import FollowUp, FollowUpAction, IncomingMessage, ParseStatus, Signal
from signal_copier.notifier import Notifier
from signal_copier.parser import SignalParser
from signal_copier.risk import RiskContext, RiskManager
from signal_copier.settings import Mode, Settings
from signal_copier.storage import Storage

logger = log.get("pipeline")

DONE = ("EXECUTED", "RECORDED")


def _fmt_signal(s: Signal) -> str:
    entry = s.entry.type.value
    if s.entry.price is not None:
        entry += f" @ {s.entry.price}"
    elif s.entry.is_zone:
        entry += f" {s.entry.zone_low}-{s.entry.zone_high}"
    tps = ", ".join(map(str, s.tp)) or "няма"
    return f"{s.symbol} {s.side.value} {entry} | SL {s.sl if s.sl is not None else 'няма'} | TP {tps}"


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        parser: SignalParser,
        notifier: Notifier,
        risk: RiskManager,
        client: OandaClient | None = None,
        executor: Executor | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if settings.mode is not Mode.RECORD and (client is None or executor is None):
            raise ValueError("paper/live mode needs an OANDA client and executor")
        self.settings = settings
        self.mode = settings.mode
        self.storage = storage
        self.parser = parser
        self.notifier = notifier
        self.risk = risk
        self.client = client
        self.executor = executor
        self.clock = clock
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- state

    def _today(self) -> str:
        return self.clock().date().isoformat()

    def halted_reason(self) -> str | None:
        if self.storage.get_state("halt_manual"):
            return "manual_stop"
        if self.storage.get_state("halt_day") == self._today():
            return "daily_loss_limit"
        return None

    def _day_start_nav(self, nav: Decimal) -> Decimal:
        key = f"day_start_nav:{self._today()}"
        value = self.storage.get_state(key)
        if value is None:
            self.storage.set_state(key, str(nav))
            return nav
        return Decimal(value)

    async def _halt_for_today(self) -> None:
        if self.storage.get_state("halt_day") != self._today():
            self.storage.set_state("halt_day", self._today())
            self.storage.add_event("halt", "DAILY_LOSS_LIMIT")
            await self.notifier.send(
                f"🛑 Достигнат дневен лимит на загуба ({self.settings.risk.daily_loss_limit_pct}%). "
                "Нови сделки спират до утре (UTC). Отворените остават."
            )

    # ---------------------------------------------------------- messages

    async def handle_message(self, msg: IncomingMessage) -> None:
        async with self._lock:
            try:
                await self._handle_message(msg)
            except Exception as e:  # never let one message kill the listener
                logger.exception("handle_message_failed", chat=msg.chat_id, msg=msg.message_id)
                self.storage.add_event("error", "FAILED", repr(e), message_key=f"{msg.chat_id}:{msg.message_id}")
                await self.notifier.send(f"❗ Грешка при обработка на съобщение {msg.message_id}: {e!r}")

    async def _handle_message(self, msg: IncomingMessage) -> None:
        mkey = f"{msg.chat_id}:{msg.message_id}"
        if not self.storage.save_message(msg):
            self.storage.add_event("message", "DUPLICATE_DELIVERY", message_key=mkey)
            return
        result = self.parser.parse(msg)
        self.storage.set_message_status(msg, result.status.value)
        logger.info("parsed", message=mkey, status=result.status.value, edited=msg.edited,
                    signals=len(result.signals), follow_ups=len(result.follow_ups))

        if result.status is ParseStatus.IGNORED:
            self.storage.add_event("parse", "IGNORED", message_key=mkey)
            return
        if result.status is ParseStatus.UNCERTAIN:
            reasons = ", ".join(i.reason for i in result.issues)
            self.storage.add_event("parse", "UNCERTAIN", reasons, message_key=mkey,
                                   data=[i.model_dump() for i in result.issues])
            for s in result.signals:
                if not self.storage.get_signal(s.key):
                    self.storage.upsert_signal(s, "REJECTED", f"uncertain_message:{reasons}")
            await self.notifier.send(
                f"⚠️ Не разбрах съобщението със сигурност ({reasons}), НЕ търгувам.\n\n{msg.text[:1500]}"
            )
            return
        for s in result.signals:
            await self.handle_signal(s)
        for fu in result.follow_ups:
            await self.handle_follow_up(fu)

    # ----------------------------------------------------------- signals

    async def handle_signal(self, s: Signal) -> None:
        existing = self.storage.get_signal(s.key)
        if existing:
            if existing["status"] in DONE:
                if s.edited:
                    await self._handle_edit(s, existing)
                else:
                    self.storage.add_event("signal", "DUPLICATE", signal_key=s.key)
                return
            if not s.edited:
                self.storage.add_event("signal", "DUPLICATE", signal_key=s.key)
                return
            # a rejected signal was edited (e.g. SL added): evaluate it again

        await self.notifier.send(f"📩 Нов сигнал{' (редактиран)' if s.edited else ''}: {_fmt_signal(s)}")

        if self.mode is Mode.RECORD:
            self.storage.upsert_signal(s, "RECORDED")
            self.storage.add_event("signal", "RECORDED", signal_key=s.key, data=s.model_dump(mode="json"))
            return

        try:
            decision, info = await self._evaluate(s)
        except Exception as e:
            logger.exception("risk_context_failed", signal=s.key)
            self.storage.upsert_signal(s, "FAILED", f"error:{e!r}")
            self.storage.add_event("risk", "FAILED", repr(e), signal_key=s.key)
            await self.notifier.send(f"❗ Не успях да проверя риска за {s.symbol}: {e!r}. Не търгувам.")
            return

        if not decision.approved:
            self.storage.upsert_signal(s, "REJECTED", decision.reason)
            self.storage.add_event("risk", "REJECTED", decision.reason, signal_key=s.key)
            await self.notifier.send(f"🚫 Отхвърлен сигнал {s.symbol} {s.side.value}: {decision.reason}")
            if decision.halt:
                await self._halt_for_today()
            return

        self.storage.upsert_signal(s, "EXECUTED", "sending")
        self.storage.add_event("risk", "APPROVED", signal_key=s.key,
                               data={"units": decision.units, "entry": decision.entry_price,
                                     "risk_amount": decision.risk_amount})
        result = await self.executor.open_signal(s, decision, info)
        status = "EXECUTED" if (result.opened or result.pending or result.skipped) else "FAILED"
        self.storage.set_signal_status(s.key, status, "; ".join(result.errors) or None)
        self.storage.add_event("execute", status, "; ".join(result.errors) or None, signal_key=s.key,
                               data={"opened": result.opened, "pending": result.pending, "skipped": result.skipped})
        lines = [f"✅ Отворена {s.symbol} {s.side.value}: trade {o['trade_id']}, {o['units']} units @ {o['price']},"
                 f" TP {o['tp']}" for o in result.opened]
        lines += [f"⏳ Чакаща поръчка {s.symbol}: {o['units']} units @ {o['price']}, TP {o['tp']}"
                  for o in result.pending]
        lines += [f"❗ {err}" for err in result.errors]
        if lines:
            await self.notifier.send("\n".join(lines))

    async def _evaluate(self, s: Signal):
        info = await self.client.instrument(s.symbol)
        bid, ask, rate = await self.client.pricing(s.symbol)
        account = await self.client.account_summary()
        nav = Decimal(account["NAV"])
        positions = self.storage.open_positions()
        ctx = RiskContext(
            now=self.clock(),
            balance=Decimal(account["balance"]),
            nav=nav,
            day_start_nav=self._day_start_nav(nav),
            bid=bid,
            ask=ask,
            quote_to_home=rate,
            instrument=info,
            open_positions=len({p["signal_key"] for p in positions}),
            open_positions_symbol=len({p["signal_key"] for p in positions if p["instrument"] == s.symbol}),
            halted_reason=self.halted_reason(),
        )
        return self.risk.check(s, ctx), info

    async def _handle_edit(self, s: Signal, existing: dict) -> None:
        if existing["symbol"] != s.symbol or existing["side"] != s.side.value:
            self.storage.add_event("edit", "IGNORED", "symbol_or_side_changed", signal_key=s.key)
            await self.notifier.send(
                f"⚠️ Каналът редактира сигнал и смени символ/посока ({existing['symbol']} {existing['side']} → "
                f"{s.symbol} {s.side.value}). Не правя нищо автоматично."
            )
            return
        old_sl = Decimal(existing["sl"]) if existing["sl"] else None
        old_tps = [Decimal(t) for t in json.loads(existing["tps"])]
        if old_sl == s.sl and old_tps == s.tp:
            self.storage.add_event("edit", "NO_CHANGE", signal_key=s.key)
            return
        self.storage.update_signal_levels(s.key, s.sl, s.tp)
        done: list[str] = []
        if self.mode is not Mode.RECORD and existing["status"] == "EXECUTED":
            info = await self.client.instrument(s.symbol)
            done = await self.executor.amend_levels(s.key, s.sl, s.tp, info)
        self.storage.add_event("edit", "AMENDED", signal_key=s.key,
                               data={"sl": s.sl, "tp": s.tp, "actions": done})
        await self.notifier.send(
            f"✏️ Редактиран сигнал {s.symbol}: SL {old_sl} → {s.sl}, TP {old_tps} → {s.tp}"
            + (f"\n" + "\n".join(done) if done else "")
        )

    # -------------------------------------------------------- follow-ups

    def _resolve_target(self, fu: FollowUp) -> tuple[dict | None, str | None]:
        candidates: list[dict] = []
        if fu.reply_to_msg_id is not None:
            candidates = [c for c in self.storage.signals_for_message(fu.chat_id, fu.reply_to_msg_id)
                          if c["status"] in DONE]
            if fu.symbol:
                candidates = [c for c in candidates if c["symbol"] == fu.symbol] or candidates
        elif fu.symbol is not None:
            since = self.clock() - timedelta(hours=self.settings.executor.follow_up_symbol_lookback_hours)
            candidates = self.storage.recent_signals_for_symbol(fu.chat_id, fu.symbol, since)
        else:
            return None, "no_reply_and_no_symbol"
        if not candidates:
            return None, "target_signal_not_found"
        if len(candidates) > 1:
            return None, "ambiguous_target"
        return candidates[0], None

    async def handle_follow_up(self, fu: FollowUp) -> None:
        fkey = self.storage.follow_up_key(fu)
        if self.storage.get_follow_up(fkey):
            self.storage.add_event("follow_up", "DUPLICATE", message_key=fkey)
            return  # e.g. the channel edited its "close" message: never close twice
        target, problem = self._resolve_target(fu)
        if target is None:
            self.storage.save_follow_up(fu, None, "REJECTED", problem)
            self.storage.add_event("follow_up", "REJECTED", problem, message_key=fkey)
            await self.notifier.send(f"⚠️ Команда {fu.action.value} без ясна сделка ({problem}). Не правя нищо.\n\n"
                                     f"{fu.raw_text[:500]}")
            return

        key = target["key"]
        label = f"{fu.action.value} за {target['symbol']} {target['side']} (msg {target['message_id']})"
        if fu.action in (FollowUpAction.TP_HIT, FollowUpAction.SL_HIT):
            self.storage.save_follow_up(fu, key, "RECORDED")
            self.storage.add_event("follow_up", "RECORDED", signal_key=key, message_key=fkey)
            await self.notifier.send(f"ℹ️ Каналът съобщава {label}")
            return
        if self.mode is Mode.RECORD or target["status"] != "EXECUTED":
            self.storage.save_follow_up(fu, key, "RECORDED")
            self.storage.add_event("follow_up", "RECORDED", signal_key=key, message_key=fkey,
                                   data={"price": fu.price, "fraction": fu.fraction})
            await self.notifier.send(f"📝 Записано: {label}" + (f" @ {fu.price}" if fu.price else ""))
            return
        try:
            info = await self.client.instrument(target["symbol"])
            done = await self.executor.apply_follow_up(fu, key, info)
        except Exception as e:
            logger.exception("follow_up_failed", follow_up=fkey)
            self.storage.save_follow_up(fu, key, "FAILED", repr(e))
            self.storage.add_event("follow_up", "FAILED", repr(e), signal_key=key, message_key=fkey)
            await self.notifier.send(f"❗ Грешка при {label}: {e!r}")
            return
        self.storage.save_follow_up(fu, key, "EXECUTED", None if done else "nothing_to_do")
        self.storage.add_event("follow_up", "EXECUTED", signal_key=key, message_key=fkey, data=done)
        await self.notifier.send(f"🔧 {label}:\n" + ("\n".join(done) if done else "няма отворени сделки/поръчки"))

    # ------------------------------------------------- monitor & commands

    async def monitor_once(self) -> None:
        if self.client is None:
            return
        opened, closed = await self.executor.sync()
        for t in opened:
            await self.notifier.send(f"✅ Изпълнена чакаща поръчка {t['instrument']}: {t['units']} @ {t['price']}")
        for t in closed:
            await self.notifier.send(
                f"🏁 Затворена {t['instrument']} trade {t['trade_id']} @ {t['close_price']}, P/L {t['realized_pl']}"
            )
        account = await self.client.account_summary()
        nav = Decimal(account["NAV"])
        if self.risk.daily_loss_hit(nav, self._day_start_nav(nav)):
            await self._halt_for_today()

    async def handle_command(self, cmd: str) -> str:
        if cmd == "/stop":
            self.storage.set_state("halt_manual", self.clock().isoformat())
            self.storage.add_event("command", "STOP")
            return "⛔ Спрях нови сделки. Отворените сделки и follow-up командите продължават. /resume за пускане."
        if cmd == "/resume":
            self.storage.set_state("halt_manual", None)
            self.storage.add_event("command", "RESUME")
            extra = " (дневният лимит още е активен до утре)" if self.halted_reason() else ""
            return f"▶️ Пуснах отново{extra}."
        if cmd == "/status":
            return await self.status_text()
        return "Команди: /status, /stop, /resume"

    async def status_text(self) -> str:
        since = datetime.combine(self.clock().date(), datetime.min.time(), tzinfo=timezone.utc)
        counts = self.storage.signal_counts_since(since)
        lines = [
            f"Режим: {self.mode.value}",
            f"Състояние: {'спрян (' + self.halted_reason() + ')' if self.halted_reason() else 'активен'}",
            f"Сигнали днес: {sum(counts.values())} " + (str(counts) if counts else ""),
            f"Отворени позиции: {len({p['signal_key'] for p in self.storage.open_positions()})}",
        ]
        if self.client is not None:
            try:
                a = await self.client.account_summary()
                nav = Decimal(a["NAV"])
                start = self._day_start_nav(nav)
                pct = (nav - start) / start * 100 if start else Decimal(0)
                lines.append(f"Баланс {a['balance']} {a.get('currency', '')}, NAV {a['NAV']}, днес {pct:.2f}%")
            except Exception as e:
                lines.append(f"OANDA недостъпна: {e!r}")
        return "\n".join(lines)
