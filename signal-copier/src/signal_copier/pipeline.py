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
from signal_copier.executor import Executor, OandaClient, Waiting
from signal_copier.models import FollowUp, FollowUpAction, IncomingMessage, ParseStatus, Signal
from signal_copier.notifier import Notifier
from signal_copier.parser import SignalParser
from signal_copier.risk import RiskContext, RiskManager
from signal_copier.settings import Mode, Settings
from signal_copier.storage import Storage

logger = log.get("pipeline")

DONE = ("EXECUTED", "RECORDED")
# applying these twice does no harm, so a FAILED one may run again when the channel edits the message
RETRYABLE = (FollowUpAction.MOVE_SL_BE, FollowUpAction.MOVE_SL, FollowUpAction.CLOSE, FollowUpAction.CANCEL)
SL_MOVES = (FollowUpAction.MOVE_SL_BE, FollowUpAction.MOVE_SL)
INFO_ONLY = (FollowUpAction.TP_HIT, FollowUpAction.SL_HIT)
# a rejected signal is evaluated again after an edit only for reasons an edit can fix, inside the age window
EDIT_FIXABLE = ("missing_sl", "uncertain_message:")
REPLY_HOPS = 3  # "Close" replying to a "TP1 hit" post that replies to the signal
UNCLEAR_TEXT = "unclear_management_text"  # parser warning: command-like text in a signal post that was left out


def _fmt_tps(tps: list[Decimal]) -> str:
    return ", ".join(map(str, tps)) or "няма"


def _command_id(f: FollowUp) -> tuple:
    """Two versions of a post have the same command: the same action, and for "move SL to <price>" the same price
    (a corrected price is a new command; the price quoted on a close is only a note)."""
    return f.action, f.price if f.action is FollowUpAction.MOVE_SL else None


def _fmt_signal(s: Signal) -> str:
    entry = s.entry.type.value
    if s.entry.price is not None:
        entry += f" @ {s.entry.price}"
    elif s.entry.is_zone:
        entry += f" {s.entry.zone_low}-{s.entry.zone_high}"
    return f"{s.symbol} {s.side.value} {entry} | SL {s.sl if s.sl is not None else 'няма'} | TP {_fmt_tps(s.tp)}"



HELP_TEXT = (
    "📋 Команди:\n"
    "/status: режим, баланс, отворени позиции и сигнали за днес\n"
    "/stop: спира новите сделки; отворените продължават да се управляват (SL, TP, затваряне)\n"
    "/resume: пуска отново новите сделки (дневният стоп остава до утре)\n"
    "/help: този списък"
)

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
        self._trade_blocked = ""  # last reason MT5 could not trade, notified once per change
        self._halt_cancel_errors = ""  # last failure to cancel pending orders after the daily stop

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
                "Нови сделки спират до утре (UTC). Отворените остават, чакащите поръчки се отменят."
            )
            await self._cancel_pending_after_halt()

    async def _cancel_pending_after_halt(self) -> None:
        """A pending order filling after the daily stop would be a new trade: remove them (again every round)."""
        cancel_all = getattr(self.executor, "cancel_all_pending", None)
        if cancel_all is None or self.mode is Mode.RECORD or not self.storage.orders_with_status("PENDING"):
            return
        done, errors = await cancel_all()
        if done:
            self.storage.add_event("halt", "PENDING_CANCELLED", data=done)
        if errors:
            self.storage.add_event("halt", "PENDING_CANCEL_FAILED", "; ".join(errors))
        new_errors = "; ".join(errors)
        if done or (errors and new_errors != self._halt_cancel_errors):
            await self.notifier.send("🛑 Дневен лимит: чакащите поръчки\n" + "\n".join(
                done + [f"❗ не успях да отменя {e}. Пробвам пак след малко; отмени я ръчно в MT5, ако може."
                        for e in errors]))
        self._halt_cancel_errors = new_errors

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
        # commands first: "Close XAUUSD" + a new XAUUSD signal in one message must not find the new one
        for fu in result.follow_ups:
            await self.handle_follow_up(fu)
        for s in result.signals:
            await self.handle_signal(s)

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
        if s.edited:
            why = self._edit_cannot_trade(s, existing)
            if why:
                self.storage.add_event("edit", "EDIT_IGNORED", why, signal_key=s.key)
                was = f", отхвърлен по-рано ({existing['reason']})" if existing else ""
                await self.notifier.send(f"⚠️ Каналът редактира сигнал {_fmt_signal(s)}{was}. Не търгувам: {why}.")
                return
            # a rejected signal was edited right after it was posted (e.g. SL added): evaluate it again

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
        # an order in unknown state may be live at the broker: keep the signal manageable (Close/BE/edits)
        status = "EXECUTED" if (result.opened or result.pending or result.skipped or result.unknown) else "FAILED"
        self.storage.set_signal_status(s.key, status, "; ".join(result.errors) or None)
        self.storage.add_event("execute", status, "; ".join(result.errors) or None, signal_key=s.key,
                               data={"opened": result.opened, "pending": result.pending, "skipped": result.skipped,
                                     "unknown": result.unknown})
        lines = [f"✅ Отворена {s.symbol} {s.side.value}: trade {o['trade_id']}, {o['units']} units @ {o['price']},"
                 f" TP {o['tp']}" for o in result.opened]
        lines += [f"⏳ Чакаща поръчка {s.symbol}: {o['units']} units @ {o['price']}, TP {o['tp']}"
                  for o in result.pending]
        lines += [f"❗ {err}" for err in result.errors]
        if lines:
            await self.notifier.send("\n".join(lines))

    def _posted_at(self, s: Signal, existing: dict | None) -> datetime:
        """When the channel posted the message (an edit never makes a signal younger)."""
        dates = [s.date, self.storage.message_posted_at(s.chat_id, s.message_id)]
        if existing:
            dates.append(datetime.fromisoformat(existing["date"]))
        return min(d for d in dates if d is not None)

    def _previous_version(self, s: Signal, existing: dict | None) -> Signal | None:
        """The signal as the last handled version of its post read (raw_text is kept up to date by edits)."""
        if not existing:
            return None
        old = IncomingMessage(chat_id=s.chat_id, message_id=s.message_id, text=existing["raw_text"], date=s.date)
        return next((o for o in self.parser.parse(old).signals if o.key == s.key), None)

    def _new_management(self, s: Signal, existing: dict | None) -> list[FollowUp]:
        """Commands the edit wrote into the signal block that the last handled version did not have, and commands
        of the post that may run again: a close/cancel/SL move that failed (❗) or a corrected SL price."""
        if not s.management:
            return []
        old = self._previous_version(s, existing)
        before = {_command_id(f) for f in old.management} if old else set()
        new = []
        for f in s.management:
            prev = self._previous_follow_up(f, existing) if existing and _command_id(f) in before else None
            if _command_id(f) not in before or (prev is not None and self._may_run_again(f, prev)):
                new.append(f)
        return new

    def _edit_cannot_trade(self, s: Signal, existing: dict | None) -> str | None:
        """Why an edited, not yet traded signal must not be traded now (None: evaluate it like a new one)."""
        if existing and existing["status"] == "FAILED":
            return "предишният опит е завършил с грешка/отказ от брокера, не го пускам отново автоматично"
        if existing and not (existing["reason"] or "").startswith(EDIT_FIXABLE):
            return f"причината ({existing['reason']}) не се оправя с редакция"
        age = (self.clock() - self._posted_at(s, existing)).total_seconds()
        if age > self.settings.risk.max_signal_age_seconds:
            return f"публикуван е преди {int(age)} s (max {self.settings.risk.max_signal_age_seconds} s)"
        new = self._new_management(s, existing)
        if new:
            return "редакцията добавя команда/резултат (" + ", ".join(f.action.value for f in new) + ")"
        # a post that quotes an order next to "Cancel"/"Closed manually" is not a new trade, edited or not
        commands = [f.action.value for f in s.management if f.action not in INFO_ONLY]
        if commands or "contains_result_text" in s.warnings:
            return "постът съдържа команда/резултат (" + ", ".join(commands or ["резултат"]) + "), не е нов сигнал"
        return None

    async def _evaluate(self, s: Signal):
        info = await self.client.instrument(s.symbol)
        bid, ask, rate = await self.client.pricing(s.symbol)
        account = await self.client.account_summary()
        nav = Decimal(account["NAV"])
        positions = self.storage.open_positions()  # pending orders always count per symbol (no stacking)
        counted = self.storage.open_positions(include_pending=self.settings.risk.count_pending_in_max_open)
        ctx = RiskContext(
            now=self.clock(),
            balance=Decimal(account["balance"]),
            nav=nav,
            day_start_nav=self._day_start_nav(nav),
            bid=bid,
            ask=ask,
            quote_to_home=rate,
            instrument=info,
            open_positions=len({p["signal_key"] for p in counted}),
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
        management = self._new_management(s, existing)
        changed = not (old_sl == s.sl and old_tps == s.tp)
        previous = self._previous_version(s, existing)
        if UNCLEAR_TEXT in s.warnings and (previous is None or UNCLEAR_TEXT not in previous.warnings):
            # "You can close now", "Closing this trade now" added to the post: not carried out, but never silent
            self.storage.add_event("edit", "EDIT_UNCLEAR", signal_key=s.key)
            await self.notifier.send(
                f"⚠️ Каналът добави в сигнала {s.symbol} текст, който прилича на команда, но не го разбрах със "
                f"сигурност. Не правя нищо по него, провери.\n\n{s.raw_text[:1000]}")
        if changed and management:
            # "TP1 hit ✅ 4485.10" next to changed numbers: which of them is a level is a guess, so none is moved
            self.storage.add_event("edit", "EDIT_IGNORED", "levels_and_management_text", signal_key=s.key)
            await self.notifier.send(
                f"⚠️ Каналът редактира сигнал {s.symbol}: смени нивата (SL {old_sl} → {s.sl}, "
                f"TP {_fmt_tps(old_tps)} → {_fmt_tps(s.tp)}) и добави команда/резултат. Не местя SL/TP автоматично, "
                f"провери.\n\n{s.raw_text[:1000]}")
        elif changed:
            done: list[str] = []
            if self.mode is not Mode.RECORD and existing["status"] == "EXECUTED":
                info = await self.client.instrument(s.symbol)
                # an SL the edit did not touch is not sent: the live one may already be at BE
                done = await self.executor.amend_levels(s.key, s.sl if s.sl != old_sl else None, s.tp, info)
            self.storage.update_signal_levels(s.key, s.sl, s.tp)  # after amend_levels: raises keep the old levels
            self.storage.add_event("edit", "AMENDED", signal_key=s.key,
                                   data={"sl": s.sl, "tp": s.tp, "actions": done})
            await self.notifier.send(
                f"✏️ Редактиран сигнал {s.symbol}: SL {old_sl} → {s.sl}, TP {_fmt_tps(old_tps)} → {_fmt_tps(s.tp)}"
                + (f"\n" + "\n".join(done) if done else "")
            )
        elif not management:
            self.storage.add_event("edit", "NO_CHANGE", signal_key=s.key)
        for fu in management:  # "CANCELLED", "Close now", "move SL to BE" written into the post itself
            await self.handle_follow_up(fu, target=self.storage.get_signal(s.key))
        if not (changed and management):  # refused levels stay refused: a later edit never applies them silently
            # the next edit is compared with this version: a command handled now is not "new" again next time
            self.storage.update_signal_text(s.key, s.raw_text)

    # -------------------------------------------------------- follow-ups

    def _reply_target(self, fu: FollowUp) -> tuple[list[dict] | None, str | None]:
        """Signals of the replied-to message, following replies to replies ("Close" -> "TP1 hit" -> signal).

        None: the chain ends at stored messages without a signal (the symbol may still find the trade).
        A chain that reaches a message the bot never stored (posted before its history) or is longer than
        REPLY_HOPS may lead to a trade the bot does not have: not found, never another trade by symbol.
        """
        mid = fu.reply_to_msg_id
        for _ in range(REPLY_HOPS):
            signals = self.storage.signals_for_message(fu.chat_id, mid)
            if signals:  # a signal message ends the walk, even a rejected one: never jump to another trade
                done = [c for c in signals if c["status"] in DONE]
                return done, None if done else "target_signal_not_found"
            if self.storage.message_posted_at(fu.chat_id, mid) is None:
                return None, "target_signal_not_found"
            mid = self.storage.message_reply_to(fu.chat_id, mid)
            if mid is None:
                return None, None
        return None, "target_signal_not_found"

    def _resolve_target(self, fu: FollowUp) -> tuple[dict | None, str | None]:
        candidates: list[dict] | None = None
        if fu.reply_to_msg_id is not None:
            candidates, problem = self._reply_target(fu)
            if problem:
                return None, problem
            if candidates is not None and fu.symbol:
                same = [c for c in candidates if c["symbol"] == fu.symbol]
                if not same:  # "Close AUDUSD" as a reply to the XAUUSD signal: two hints disagree, do nothing
                    names = "/".join(sorted({c["symbol"] for c in candidates}))
                    return None, f"symbol_mismatch:{fu.symbol}≠{names}"
                candidates = same
        if candidates is None:
            if fu.symbol is None:
                return None, "target_signal_not_found" if fu.reply_to_msg_id is not None else "no_reply_and_no_symbol"
            since = self.clock() - timedelta(hours=self.settings.executor.follow_up_symbol_lookback_hours)
            if self.mode is Mode.RECORD:  # nothing is traded: the latest signals in the window
                candidates = self.storage.recent_signals_for_symbol(fu.chat_id, fu.symbol, since)
            else:  # only signals that still have an open trade or a pending order, however old
                candidates = self.storage.open_signals_for_symbol(fu.chat_id, fu.symbol)
                if not candidates and fu.action in INFO_ONLY:  # "SL hit" after the trade closed
                    candidates = self.storage.recent_signals_for_symbol(fu.chat_id, fu.symbol, since)[:1]
            # a command is only about a signal posted before it (channel message ids only grow): never the new
            # signal of its own message, and never a trade opened after it (an edit hours later must not find one)
            candidates = [c for c in candidates if c["message_id"] < fu.message_id]
        if not candidates:
            return None, "target_signal_not_found"
        if len(candidates) > 1:
            return None, "ambiguous_target"
        return candidates[0], None

    @staticmethod
    def _same_command(fu: FollowUp, target: dict | None, row: dict) -> bool:
        """Whether a stored row is this command. One message can hold two with the same key: "Close XAUUSD" for an
        older trade and "Close now" written under the new XAUUSD signal of the same post (target). A command
        written into a signal's post is only ever about that signal; any other is never about a signal of its own
        message (see _resolve_target)."""
        if target is not None:
            return row["signal_key"] == target["key"]
        return not (row["signal_key"] or "").startswith(f"{fu.chat_id}:{fu.message_id}:")

    def _command_key(self, fu: FollowUp, target: dict | None) -> str:
        """The row of this command: the usual key, unless the other command of the message already holds it."""
        key = self.storage.follow_up_key(fu)
        row = self.storage.get_follow_up(key)
        if row is None or self._same_command(fu, target, row):
            return key
        return f"{key}>{target['key']}" if target is not None else f"{key}>message"

    def _previous_follow_up(self, fu: FollowUp, target: dict | None = None) -> dict | None:
        prev = self.storage.get_follow_up(self._command_key(fu, target))
        if prev is None and fu.symbol is not None:  # written before the symbol was part of the key
            old = self.storage.get_follow_up(self.storage.follow_up_key(fu, legacy=True))
            old_target = self.storage.get_signal(old["signal_key"]) if old and old["signal_key"] else None
            if old and self._same_command(fu, target, old) and (old_target is None
                                                                or old_target["symbol"] == fu.symbol):
                prev = old
        if prev is None and fu.symbol is None:
            # "Close 50% of XAUUSD now" edited to "Close 50% now": the same command, stored under its symbol. A
            # rejected row did nothing: "Cancel Pending Order on AUDUSD" corrected to "Cancel Pending Order" runs
            rows = [r for r in self.storage.follow_ups_for(fu.chat_id, fu.message_id, fu.action.value)
                    if r["status"] != "REJECTED" and self._same_command(fu, target, r)]
            prev = next((r for r in rows if not self._may_run_again(fu, r)), rows[0] if rows else None)
        return prev

    @staticmethod
    def _may_run_again(fu: FollowUp, prev: dict) -> bool:
        if prev["status"] == "FAILED" and fu.action in RETRYABLE:
            return True
        if fu.action in SL_MOVES:  # an edit that corrects the price, or a first attempt that did not go through
            old_price = Decimal(prev["price"]) if prev["price"] else None
            return prev["status"] in ("FAILED", "REJECTED") or old_price != fu.price
        return False

    async def handle_follow_up(self, fu: FollowUp, target: dict | None = None) -> None:
        """target: the signal, when the command was written into that signal's own post (an edit)."""
        fkey = self._command_key(fu, target)
        prev = self._previous_follow_up(fu, target)
        if prev and not self._may_run_again(fu, prev):
            self.storage.add_event("follow_up", "DUPLICATE", message_key=fkey)
            if fu.edited:  # e.g. the channel edited its "close" message: never close twice, but say so
                await self.notifier.send(f"ℹ️ Каналът редактира команда {fu.action.value} (msg {fu.message_id}): "
                                         f"вече е обработена ({prev['status']}), не я повтарям.")
            return
        problem = None
        if prev:
            self.storage.add_event("follow_up", "RETRY", prev["reason"], message_key=fkey)
            if target is None and prev["signal_key"]:
                # a retry acts on the trade the command was about, never on one the symbol finds now
                target = self.storage.get_signal(prev["signal_key"])
                problem = None if target else "target_gone"
        if target is None and problem is None:
            target, problem = self._resolve_target(fu)
        if target is None:
            self.storage.save_follow_up(fu, None, "REJECTED", problem, key=fkey)
            self.storage.add_event("follow_up", "REJECTED", problem, message_key=fkey)
            await self.notifier.send(f"⚠️ Команда {fu.action.value} без ясна сделка ({problem}). Не правя нищо.\n\n"
                                     f"{fu.raw_text[:500]}")
            return

        key = target["key"]
        label = f"{fu.action.value} за {target['symbol']} {target['side']} (msg {target['message_id']})"
        if fu.action in INFO_ONLY:
            self.storage.save_follow_up(fu, key, "RECORDED", key=fkey)
            self.storage.add_event("follow_up", "RECORDED", signal_key=key, message_key=fkey)
            await self.notifier.send(f"ℹ️ Каналът съобщава {label}")
            return
        if self.mode is Mode.RECORD or target["status"] != "EXECUTED":
            self.storage.save_follow_up(fu, key, "RECORDED", key=fkey)
            self.storage.add_event("follow_up", "RECORDED", signal_key=key, message_key=fkey,
                                   data={"price": fu.price, "fraction": fu.fraction})
            await self.notifier.send(f"📝 Записано: {label}" + (f" @ {fu.price}" if fu.price else ""))
            return
        try:
            info = await self.client.instrument(target["symbol"])
            done = await self.executor.apply_follow_up(fu, key, info)
        except Exception as e:
            logger.exception("follow_up_failed", follow_up=fkey)
            self.storage.save_follow_up(fu, key, "FAILED", repr(e), key=fkey)
            self.storage.add_event("follow_up", "FAILED", repr(e), signal_key=key, message_key=fkey)
            await self.notifier.send(f"❗ Грешка при {label}: {e!r}")
            return
        waiting = any(isinstance(d, Waiting) for d in done)
        status = "WAITING" if waiting else "EXECUTED"
        self.storage.save_follow_up(fu, key, status, None if done else "nothing_to_do", key=fkey)
        self.storage.add_event("follow_up", status, signal_key=key, message_key=fkey, data=done)
        await self.notifier.send(f"🔧 {label}:\n" + ("\n".join(done) if done else "няма отворени сделки/поръчки"))

    async def _retry_waiting(self) -> None:
        """Commands that could not be applied yet: an SL move while the price was behind the entry, or a command for
        an order still without answer (applied once it turns up; it ends when the order is given up)."""
        for row in self.storage.follow_ups_with_status("WAITING"):
            target = self.storage.get_signal(row["signal_key"]) if row["signal_key"] else None
            fu = FollowUp(chat_id=row["chat_id"], message_id=row["message_id"], action=FollowUpAction(row["action"]),
                          price=Decimal(row["price"]) if row["price"] else None, raw_text="",
                          date=datetime.fromisoformat(row["date"]))
            if target is None:
                self.storage.update_follow_up(row["key"], "REJECTED", "target_gone")
                continue
            label = f"{fu.action.value} за {target['symbol']} {target['side']} (msg {target['message_id']})"
            try:
                info = await self.client.instrument(target["symbol"])
                done = await self.executor.apply_follow_up(fu, target["key"], info)
            except Exception as e:  # MT5 busy or down: keep waiting, the next round tries again
                logger.warning("waiting_follow_up_failed", follow_up=row["key"], error=repr(e))
                continue
            if any(isinstance(d, Waiting) for d in done):
                continue
            self.storage.update_follow_up(row["key"], "EXECUTED", None if done else "nothing_to_do")
            self.storage.add_event("follow_up", "EXECUTED", "after_waiting", signal_key=target["key"],
                                   message_key=row["key"], data=done)
            await self.notifier.send(f"🔧 {label} (изчакано):\n" + ("\n".join(done) if done else
                                                                    "няма отворена сделка или чакаща поръчка"))

    # ------------------------------------------------- monitor & commands

    async def monitor_once(self) -> None:
        if self.client is None:
            return
        async with self._lock:  # never between an order_send and its rows: a new position would look unknown
            opened, closed = await self.executor.sync()
        for t in opened:
            kind = t.get("kind", "filled")
            if kind == "untracked":
                text = (f"⚠️ В MT5 има {t['what']} на бота, която ботът не следи: {t['instrument']} #{t['trade_id']}, "
                        f"{t['units']} лота. Може да е поръчката без отговор ({t['client_ids']}), затова НЕ я отписвам "
                        "като неизпълнена и мястото ѝ остава заето. Провери в MT5 и я затвори/отмени ръчно, ако не я "
                        "искаш.")
            elif kind == "recovered":
                text = (f"✅ Поръчката без отговор е изпълнена в MT5: {t['instrument']} trade {t['trade_id']}, "
                        f"{t['units']} @ {t['price']}. Ботът я управлява.")
            elif kind == "recovered_closed":
                text = (f"✅ Поръчката без отговор е била изпълнена в MT5 и вече е затворена: {t['instrument']} "
                        f"trade {t['trade_id']}, {t['units']} @ {t['price']}.")
            elif kind == "pending":
                text = (f"⏳ Поръчката без отговор е чакаща в MT5: {t['instrument']} #{t['trade_id']}, "
                        f"{t['units']} @ {t['price']}. Ботът я управлява.")
            elif kind == "expired":
                self.storage.add_event("execute", "UNKNOWN_EXPIRED", signal_key=t["signal_key"],
                                       data={"client_id": t["trade_id"], "minutes": t["minutes"]})
                text = (f"⌛ Поръчката без отговор {t['instrument']} ({t['units']} лота, {t['trade_id']}) не се появи "
                        f"в MT5 за {t['minutes']} мин (проверени позиции, чакащи поръчки и сделки). Приемам, че НЕ е "
                        "изпълнена: отбелязвам я като неуспешна и освобождавам мястото ѝ. Ако все пак се появи в MT5, "
                        "ботът ще я поеме.")
            else:
                text = f"✅ Изпълнена чакаща поръчка {t['instrument']}: {t['units']} @ {t['price']}"
                if self.storage.get_state("halt_day") == self._today():
                    text += " ⚠️ след дневния лимит: не успях да я отменя навреме, позицията е със SL/TP"
            await self.notifier.send(text)
        for t in closed:
            await self.notifier.send(
                f"🏁 Затворена {t['instrument']} trade {t['trade_id']} @ {t['close_price']}, P/L {t['realized_pl']}"
            )
        async with self._lock:  # these act on positions: never in the middle of a channel command
            await self._retry_waiting()
        await self._check_trade_allowed()
        account = await self.client.account_summary()
        nav = Decimal(account["NAV"])
        async with self._lock:
            halted = self.storage.get_state("halt_day") == self._today()
            if self.risk.daily_loss_hit(nav, self._day_start_nav(nav)):
                await self._halt_for_today()  # the first time it also cancels the pending orders
            if halted:  # every round after: one that could not be cancelled is tried again
                await self._cancel_pending_after_halt()

    async def _check_trade_allowed(self) -> None:
        """MT5: tell the owner once when the terminal stops being able to trade (Algo Trading off ...) and back."""
        blockers_fn = getattr(self.client, "trade_blockers", None)
        if blockers_fn is None:
            return
        blockers = await blockers_fn()
        if blockers is None:
            return  # MT5 did not answer: account_summary below reports it
        blocked = "; ".join(blockers)
        if blocked != self._trade_blocked:
            self._trade_blocked = blocked
            self.storage.add_event("mt5", "TRADE_BLOCKED" if blocked else "TRADE_ALLOWED", blocked or None)
            await self.notifier.send(f"🚫 MT5 не може да търгува: {blocked}. Новите сигнали и командите ще се "
                                     "отказват, докато не го оправиш." if blocked else "✅ MT5 отново може да търгува.")

    async def handle_command(self, cmd: str) -> str:
        """/help, /start, /commands and anything unknown return HELP_TEXT."""
        if cmd == "/stop":
            self.storage.set_state("halt_manual", self.clock().isoformat())
            self.storage.add_event("command", "STOP")
            pending = self.storage.orders_with_status("PENDING")
            extra = (f"\n⏳ Чакащи поръчки в MT5: {len(pending)} ("
                     + ", ".join(f"{o['instrument']} #{o['oanda_order_id']}" for o in pending)
                     + "). Те остават и могат да се изпълнят; отмени ги в MT5, ако не ги искаш.") if pending else ""
            return ("⛔ Спрях нови сделки. Отворените сделки и follow-up командите продължават. /resume за пускане."
                    + extra)
        if cmd == "/resume":
            self.storage.set_state("halt_manual", None)
            self.storage.add_event("command", "RESUME")
            extra = " (дневният лимит още е активен до утре)" if self.halted_reason() else ""
            return f"▶️ Пуснах отново{extra}."
        if cmd == "/status":
            return await self.status_text()
        return HELP_TEXT

    async def status_text(self) -> str:
        since = datetime.combine(self.clock().date(), datetime.min.time(), tzinfo=timezone.utc)
        counts = self.storage.signal_counts_since(since)
        lines = [
            f"Режим: {self.mode.value}",
            f"Състояние: {'спрян (' + self.halted_reason() + ')' if self.halted_reason() else 'активен'}",
            f"Сигнали днес: {sum(counts.values())} " + (str(counts) if counts else ""),
            f"Отворени позиции: {len({p['signal_key'] for p in self.storage.open_positions(include_pending=False)})}"
            f", чакащи поръчки: {len(self.storage.orders_with_status('PENDING'))}",
        ]
        if self.client is not None:
            try:
                a = await self.client.account_summary()
                nav = Decimal(a["NAV"])
                start = self._day_start_nav(nav)
                pct = (nav - start) / start * 100 if start else Decimal(0)
                lines.append(f"Баланс {a['balance']} {a.get('currency', '')}, NAV {a['NAV']}, днес {pct:.2f}%")
            except Exception as e:
                lines.append(f"{self.settings.broker.value.upper()} недостъпен: {e!r}")
        return "\n".join(lines)
