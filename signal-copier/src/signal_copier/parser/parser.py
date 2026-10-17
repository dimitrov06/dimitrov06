"""Text -> Signal / FollowUp.

Policy: when in doubt, do not trade. Anything that looks trade-related but
cannot be parsed confidently becomes a ParseIssue (status UNCERTAIN), and
nothing from that message is traded.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from signal_copier.models import (
    Entry,
    EntryType,
    FollowUp,
    FollowUpAction,
    IncomingMessage,
    ParseIssue,
    ParseResult,
    Side,
    Signal,
)
from signal_copier.parser.rules import ParserRules
from signal_copier.parser.symbols import SymbolMapper

NUM = r"\d+(?:\.\d+)?"
INFO_ACTIONS = (FollowUpAction.TP_HIT, FollowUpAction.SL_HIT)  # reports: never guarded, never traded
CLOSE_ACTIONS = (FollowUpAction.CLOSE, FollowUpAction.CLOSE_PARTIAL)
CLAUSE_END = re.compile(r"[,;:!?\n]|\.(?!\d)")
SENTENCE_END = re.compile(r"[;!?\n]|\.(?!\d)")
CLOSE_PRICE = re.compile(r"(?:\bAT|@)[ \t]*\d+(?:\.\d+)?(?![\d:])")
CLOSE_SIZE = re.compile(r"\b1\s*/\s*2\b|\b\d{1,3}\s*PERCENT\b")
COMMAND_NOW = re.compile(r"[ \t]*(?:NOW|IMMEDIATELY|ASAP)\b")
BRACKET_PERCENT = re.compile(r"\((?![^)]*[+\-])[^)]*(?<![\d.])\d{1,3}\s*%")  # "(43%)", not "(+1.43%)" / "(1.43%)"
# a size after a full close in its sentence that the partial close did not take: "Close XAUUSD now, half position",
# "Close XAUUSD, 50% now" (a partial close?) or "Close XAUUSD now, 50% of the target reached" (a note?): not guessed.
# A P/L ("2% in profit", "(+0.50%)") is no size
SIZE_AFTER_CLOSE = re.compile(
    r"(?<![\d.+\-(])\d{1,3}(?![\d.])\s*(?:%|PERCENT\b)"
    r"(?![ \t]*(?:IN[ \t]+)?(?:THE[ \t]+)?(?:PROFIT|GAIN|LOSS|UP|DOWN|ROI|RETURN|GREEN|RED)\b)"
    r"|\b(?:HALF|PARTIAL(?:LY)?|PARTIALS|1\s*/\s*2)\b")
PRICE_OR_ZONE = re.compile(rf"(?P<a>{NUM})(?:[ \t]*(?:-|/|~|\bTO\b)[ \t]*(?P<b>{NUM}))?")
NUMBER = re.compile(NUM)
DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
DROP_CATEGORIES = {"So", "Sk", "Cf", "Cs", "Co", "Mn"}


@dataclass
class TpNotes:
    hit: bool = False  # a hit word in a TP line ("TP1 ✅ 4484.30" too)
    result: bool = False  # a TP line reports a hit ("Target Profit 1 @ 4484.30 ✅", "TP1 hit")
    plain: bool = False  # a TP line gives a level without any hit word
    repeated: bool = False  # a label given a second, different level without a hit word ("TP1 @ x / TP1 @ y")
    by_label: bool = False  # every level has a label and they are in label order (not as written)


@dataclass
class Commands:
    follow_ups: list[FollowUp] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # reasons the commands must not be guessed
    dropped: bool = False  # a negated, conditional or unclear command was left out
    spans: list[tuple[int, int]] = field(default_factory=list)  # where commands are written in the block


class SignalParser:
    def __init__(
        self,
        rules: ParserRules | None = None,
        bare_price_entry: str = "market",
        max_price_deviation_pct: float = 10.0,
    ):
        self.rules = rules or ParserRules.load()
        self.symbols = SymbolMapper(self.rules.raw.symbols, self.rules.raw.fx_currencies)
        if bare_price_entry not in ("market", "limit"):
            raise ValueError("bare_price_entry must be 'market' or 'limit'")
        self.bare_price_entry = bare_price_entry
        self.max_dev = Decimal(str(max_price_deviation_pct))

    # ------------------------------------------------------------------ public

    def parse(self, msg: IncomingMessage) -> ParseResult:
        result = ParseResult(message=msg)
        text = self.normalize(msg.text)
        for block in self._split_blocks(text):
            self._parse_block(block, msg, result, signal_index=len(result.signals))
        return result

    def normalize(self, text: str) -> str:
        for emoji, repl in self.rules.emoji_replacements.items():
            text = text.replace(emoji, repl)
        text = unicodedata.normalize("NFKC", text).translate(DASHES).upper()
        text = "".join(" " if unicodedata.category(c) in DROP_CATEGORIES else c for c in text)
        text = re.sub(r"[*_`#]", " ", text)
        text = re.sub(r"\b([A-Z]{3})[ \t]*/[ \t]*([A-Z]{3})\b", r"\1\2", text)  # XAU/USD
        text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)  # 2,345.50 -> 2345.50
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    # ---------------------------------------------------------------- blocks

    def _split_blocks(self, text: str) -> list[str]:
        """A new block starts at each line naming a symbol, once the current block has one."""
        blocks: list[list[str]] = [[]]
        has_symbol = False
        for line in text.splitlines():
            line_has_symbol = self.symbols.find(line) is not None
            if line_has_symbol and has_symbol:
                blocks.append([])
                has_symbol = False
            blocks[-1].append(line)
            has_symbol = has_symbol or line_has_symbol
        return ["\n".join(b) for b in blocks if b]

    def _parse_block(self, block: str, msg: IncomingMessage, result: ParseResult, signal_index: int) -> None:
        symbol = self.symbols.find(block)
        sides = {s for s, pats in self.rules.side.items() if any(p.search(block) for p in pats)}
        cmds = self._commands(block, msg, symbol.instrument if symbol else None)
        follow_ups = cmds.follow_ups
        sl = self._find_sl(block)
        tps, tp_notes = self._scan_tps(block)

        if len(sides) > 1:
            result.issues.append(ParseIssue(reason="conflicting_sides", text=block))
            return

        levels = sl is not None or bool(tps)
        entry_line = self._has_entry_instruction(self._blank(block, cmds.spans))  # "move SL to BE now" is no entry
        reported = self._reports(block, follow_ups, tp_notes)
        updating = any(p.search(block) for p in self.rules.update_words)
        # "XAUUSD Buy update / TP1 @ 4484.30 hit ✅ / Stop Loss @ 4479.69 (moved to entry)", "XAUUSD Buy new SL 4475":
        # a side word and a level, but nothing that says to open a trade -> about a running trade, never a new one
        update = bool(sides) and levels and not entry_line and bool(
            follow_ups or cmds.problems or cmds.dropped or reported or tp_notes.hit or updating)

        management = not sides or (sl is None and not tps) or update
        if management:
            issue = self._management_issue(block, follow_ups, cmds)
            if issue:
                result.issues.append(ParseIssue(reason=issue, text=block))
                return

        if update:
            self._update_block(block, result, follow_ups, sl, tp_notes, reported, updating)
            return

        if sides:
            if sl is None and not tps:
                if follow_ups:
                    # "Gold buy running, move SL to BE" is management, not a new trade
                    result.follow_ups.extend(follow_ups)
                    return
                if not entry_line and (reported or updating):
                    return  # "XAUUSD Buy running nicely": chatter about a trade
            elif not entry_line and not tps:
                # "XAUUSD Buy - SL @ 4475": no entry line and no TP, nothing says to open a trade
                result.issues.append(ParseIssue(reason="incomplete_signal", text=block))
                return
            elif not entry_line and msg.reply_to_msg_id is not None:
                # a reply that repeats levels without an entry line is about the replied trade, not a new one
                result.issues.append(ParseIssue(reason="reply_without_entry", text=block))
                return
            if symbol is None:
                result.issues.append(ParseIssue(reason="unknown_symbol", text=block))
                return
            signal_or_issue = self._build_signal(block, msg, symbol, Side(sides.pop()), sl, tps, tp_notes,
                                                 signal_index)
            if isinstance(signal_or_issue, ParseIssue):
                result.issues.append(signal_or_issue)
                return
            if follow_ups or cmds.dropped:
                signal_or_issue.warnings.append("contains_management_text")
                signal_or_issue.management = follow_ups
            if cmds.problems:  # command-like text that was left out ("You can close now"): told on an edit
                signal_or_issue.warnings.append("unclear_management_text")
            if reported or updating:
                signal_or_issue.warnings.append("contains_result_text")
            result.signals.append(signal_or_issue)
            if not msg.edited:
                problem = self._new_post_problem(follow_ups, tps, reported, updating)
                if problem:
                    # kept in signals so a later edit can tell which commands the post already had
                    result.issues.append(ParseIssue(reason=problem, text=block))
            return

        if follow_ups:
            result.follow_ups.extend(follow_ups)
            return

        if sl is not None or tps or self.rules.sl_keyword.search(block):
            result.issues.append(ParseIssue(reason="missing_side", text=block))
        # otherwise: chatter / results post -> ignored

    def _management_issue(self, block: str, follow_ups: list[FollowUp], cmds: Commands) -> str | None:
        """Why a message without a new signal must not be acted on (None: carry out its commands)."""
        if any(p.search(block) for p in self.rules.unclear):
            return "unclear_sl_command"  # "move SL to 20 pips in profit", "SL to 1:1": a distance, not a price
        if cmds.problems:
            return cmds.problems[0]  # "close half at TP1", "Closing XAUUSD", "Close at 23:00"
        actionable = [f for f in follow_ups if f.action not in INFO_ACTIONS]
        if actionable and len({m.instrument for m in self.symbols.find_all(block)}) > 1:
            return "command_for_several_symbols"  # "Close AUDUSD and XAUUSD now": not guessed
        return None

    def _update_block(self, block, result: ParseResult, follow_ups, sl, tp_notes: TpNotes, reported, updating) -> None:
        """A side word and levels without an entry line: the channel talks about a running trade."""
        if isinstance(sl, ParseIssue):
            result.issues.append(sl)
            return
        actionable = [f for f in follow_ups if f.action not in INFO_ACTIONS]
        moves = [f.price for f in actionable if f.action is FollowUpAction.MOVE_SL]
        if sl is not None and not (moves and all(m == sl for m in moves)):
            # "Stop Loss @ 4479.69 (moved to entry)", "close half / Stop Loss @ x": which SL is meant is not guessed
            result.issues.append(ParseIssue(reason="update_with_levels", text=block))
            return
        if tp_notes.plain:
            # "XAUUSD Buy move SL to BE / TP2 @ 4500": a new TP is not guessed
            result.issues.append(ParseIssue(reason="update_with_levels", text=block))
            return
        if not actionable and (not reported or updating or self.rules.sl_keyword.search(block)):
            # "XAUUSD Buy / TP1 @ 4484.30 ✅ / Update the SL to the entry price" with no command the bot knows
            result.issues.append(ParseIssue(reason="update_not_understood", text=block))
            return
        result.follow_ups.extend(follow_ups)  # "XAUUSD Buy running ✅ TP1 @ 4484.30 reached, move SL to BE"

    @staticmethod
    def _new_post_problem(follow_ups: list[FollowUp], tps, reported: bool, updating: bool) -> str | None:
        """A new (not edited) post with a whole signal that is not a plain new trade."""
        if reported or any(f.action in (FollowUpAction.CLOSE, FollowUpAction.CANCEL) for f in follow_ups):
            return "signal_with_result_text"  # a repost "... Closed manually at 4478 ❌", a cancel quoting the order
        if updating or (not tps and any(f.action not in INFO_ACTIONS for f in follow_ups)):
            return "signal_with_update_text"  # "XAUUSD Buy 4480 running, new SL 4475"
        return None

    def _reports(self, block: str, follow_ups: list[FollowUp], tp_notes: TpNotes) -> bool:
        """The block reports what already happened (TP/SL hit, closed, cancelled). "TP1 ✅ 2350" is a level."""
        return (tp_notes.result or any(f.action is FollowUpAction.SL_HIT for f in follow_ups)
                or any(p.search(block) for p in self.rules.results))

    def _has_entry_instruction(self, block: str) -> bool:
        """An order type, a market word or an entry price: the block says to open a trade."""
        if any(p.search(block) for pats in self.rules.pending_order.values() for p in pats):
            return True
        if any(p.search(block) for p in self.rules.market_words):
            return True
        return self._find_entry(block) != (None, None)

    # ---------------------------------------------------------------- signal

    def _build_signal(
        self, block, msg, symbol, side: Side, sl, tps, tp_notes: TpNotes, index
    ) -> Signal | ParseIssue:
        warnings: list[str] = []
        if isinstance(sl, ParseIssue):
            return sl

        pending_types = {t for t, pats in self.rules.pending_order.items() if any(p.search(block) for p in pats)}
        is_market = any(p.search(block) for p in self.rules.market_words)
        if len(pending_types) > 1 or self._unrecognised_order_type(block, is_market or bool(pending_types)):
            # "Buy Stp Order @", "Sell Limt Order @", "Buy Stop Limit @": never taken at market by mistake
            return ParseIssue(reason="unclear_entry_type", text=block)
        pending = next(iter(pending_types), None)
        price, zone = self._find_entry(block)

        if pending and price is None and zone is None:
            return ParseIssue(reason="pending_order_without_price", text=block)
        if pending:
            entry_type = EntryType(pending)
        elif is_market or (price is None and zone is None) or self.bare_price_entry == "market":
            entry_type = EntryType.MARKET
        else:
            entry_type = EntryType.LIMIT

        if zone is not None:
            low, high = zone
            entry = Entry(type=entry_type, zone_low=low, zone_high=high)
        else:
            entry = Entry(type=entry_type, price=price)

        if sl is None:
            warnings.append("missing_sl")
        if not tps:
            warnings.append("missing_tp")

        if tp_notes.repeated:
            return ParseIssue(reason="repeated_tp_label", text=block)  # "TP1 @ x / TP1 @ y": which one is TP2?
        if tp_notes.by_label and len(tps) > 1 and tps != sorted(tps, reverse=side is Side.SELL):
            # "TP2 @ 4484.30 / TP1 @ 4498.30" on a buy: the labels and the prices disagree on the last target
            return ParseIssue(reason="tp_labels_out_of_order", text=block)
        problem = self._sanity_check(side, entry, sl, tps)
        if problem:
            return ParseIssue(reason=problem, text=block)

        return Signal(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            index=index,
            symbol=symbol.instrument,
            raw_symbol=symbol.raw,
            side=side,
            entry=entry,
            sl=sl,
            tp=tps,
            raw_text=msg.text,
            date=msg.date,
            edited=msg.edited,
            warnings=warnings,
        )

    def _unrecognised_order_type(self, block: str, typed: bool) -> bool:
        """The entry line names an order type that pending_order does not know (a typo or another wording).

        `typed`: a market word or a recognised pending type is there, so ORDER alone is fine ("Buy Market Order").
        """
        for line in self._entry_lines(block):
            for pats in self.rules.pending_order.values():
                for p in pats:
                    line = p.sub(" ", line)  # what is left of "Buy Stop Limit Order" after "BUY STOP" is "LIMIT"
            if self.rules.pending_hint and self.rules.pending_hint.search(line):
                return True
            if self.rules.order_word and not typed and self.rules.order_word.search(line):
                return True
        return False

    def _entry_lines(self, block: str) -> list[str]:
        """Lines with BUY/SELL or "@", cut before any SL/TP keyword."""
        lines = []
        for line in block.splitlines():
            cut = len(line)
            for kw in (self.rules.sl_keyword, self.rules.tp_keyword):
                m = kw.search(line)
                if m:
                    cut = min(cut, m.start())
            line = line[:cut]
            if "@" in line or any(p.search(line) for pats in self.rules.side.values() for p in pats):
                lines.append(line)
        return lines

    def _find_sl(self, block: str) -> Decimal | ParseIssue | None:
        values = {Decimal(m.group("price")) for line in block.splitlines() for m in self.rules.sl.finditer(line)}
        if len(values) > 1:
            return ParseIssue(reason="multiple_sl_values", text=block)
        return values.pop() if values else None

    def _find_tps(self, block: str) -> list[Decimal]:
        return self._scan_tps(block)[0]

    def _scan_tps(self, block: str) -> tuple[list[Decimal], TpNotes]:
        """TP levels in order. "TP2" is the second level wherever it is written (the "last" TP is the highest label).

        A result note is not a level: numbers after HIT are dropped ("Target Profit 1 @ 4484.30 ✅ hit at 4485.10"
        -> 4484.30), and a label that already appeared ignores later lines ("TP1 hit ✅ 4485.10" under the TP lines).
        The notes say whether a TP line reports a hit ("4484.30 ✅", "TP1 hit") and whether one is a plain level.
        """
        found: list[tuple[int | None, Decimal]] = []
        labels: set[int] = set()
        notes = TpNotes()
        for line in block.splitlines():
            if not self.rules.tp_keyword.search(line):
                continue
            line = self.rules.sl.sub(" ", line)
            line = self._strip_noise(line)
            keywords = list(self.rules.tp_keyword.finditer(line))
            for i, kw in enumerate(keywords):
                segment = line[kw.end(): keywords[i + 1].start() if i + 1 < len(keywords) else len(line)]
                digit = re.search(r"\d", kw.group())
                label = int(digit.group()) if digit else None
                hit = self.rules.tp_hit.search(segment)
                notes.hit = notes.hit or bool(hit)
                if label is not None and label in labels:
                    notes.result = notes.result or bool(hit)
                    if not hit and any(Decimal(n) not in {v for _, v in found} for n in NUMBER.findall(segment)):
                        notes.repeated = True  # "Target Profit 1 @ 4498.30" again (TP2 copy-pasted?): not guessed
                    continue  # "TP1 hit ✅ 4485.10" under the TP1 line: a result, not a new level
                if hit and NUMBER.search(segment[: hit.start()]) or hit and not NUMBER.search(segment):
                    notes.result = True  # "Target Profit 1 @ 4484.30 ✅", "TP1 hit"
                if hit and (label is None or NUMBER.search(segment[: hit.start()])):
                    segment = segment[: hit.start()]  # "TP1 4484.30 hit at 4485.10" ("TP1 ✅ 4484.30" is a level)
                numbers = NUMBER.findall(segment)
                notes.plain = notes.plain or bool(numbers and not hit)
                for n in numbers:
                    value = Decimal(n)
                    if all(value != v for _, v in found):
                        found.append((label, value))
                if label is not None:
                    labels.add(label)
        if found and all(label is not None for label, _ in found):
            found.sort(key=lambda x: x[0])
            notes.by_label = True
        return [v for _, v in found], notes

    def _find_entry(self, block: str) -> tuple[Decimal | None, tuple[Decimal, Decimal] | None]:
        lines = block.splitlines()
        # 1) explicit "ENTRY 2345" / "@ 2345-2350"
        for line in lines:
            if self.rules.sl_keyword.search(line) or self.rules.tp_keyword.search(line):
                continue  # "Target Profit 1 @ 0.69446" is not the entry
            line = self._strip_noise(line)
            m = re.search(self.rules.entry.pattern + PRICE_OR_ZONE.pattern, line)
            if m:
                return self._price_or_zone(m)
        # 2) number on the BUY/SELL line: "XAUUSD BUY 2345", "GOLD SELL 2350-53"
        for line in lines:
            if not any(p.search(line) for pats in self.rules.side.values() for p in pats):
                continue
            cut = len(line)
            for kw in (self.rules.sl_keyword, self.rules.tp_keyword):
                m = kw.search(line)
                if m:
                    cut = min(cut, m.start())
            rest = self._strip_noise(self.symbols.remove(line[:cut]))
            m = PRICE_OR_ZONE.search(rest)
            if m:
                return self._price_or_zone(m)
        return None, None

    @staticmethod
    def _price_or_zone(m: re.Match) -> tuple[Decimal | None, tuple[Decimal, Decimal] | None]:
        a = Decimal(m.group("a"))
        b_raw = m.group("b")
        if b_raw is None:
            return a, None
        a_int = m.group("a").split(".")[0]
        if "." not in b_raw and "." not in m.group("a") and len(b_raw) < len(a_int):
            b_raw = a_int[: len(a_int) - len(b_raw)] + b_raw  # 2350-53 -> 2350-2353
        try:
            b = Decimal(b_raw)
        except InvalidOperation:
            return a, None
        if a == b:
            return a, None
        return None, (min(a, b), max(a, b))

    def _strip_noise(self, line: str) -> str:
        for p in self.rules.noise:
            line = p.sub(" ", line)
        return line

    def _sanity_check(self, side: Side, entry: Entry, sl: Decimal | None, tps: list[Decimal]) -> str | None:
        ref = entry.reference_price
        lo = entry.zone_low if entry.is_zone else entry.price
        hi = entry.zone_high if entry.is_zone else entry.price
        buy = side is Side.BUY

        if lo is not None:
            if sl is not None and (sl >= lo if buy else sl <= hi):
                return "sl_wrong_side_of_entry"
            if any((tp <= hi if buy else tp >= lo) for tp in tps):
                return "tp_wrong_side_of_entry"
        elif sl is not None and tps:
            if any((tp <= sl if buy else tp >= sl) for tp in tps):
                return "tp_wrong_side_of_sl"

        anchor = ref if ref is not None else sl
        if anchor:
            for value in ([sl] if sl is not None else []) + tps:
                if abs(value - anchor) / anchor * 100 > self.max_dev:
                    return "price_out_of_range"
        if entry.is_zone and (entry.zone_high - entry.zone_low) / entry.zone_low * 100 > self.max_dev:
            return "price_out_of_range"
        return None

    # ------------------------------------------------------------- follow-ups

    def _commands(self, block: str, msg: IncomingMessage, symbol: str | None) -> Commands:
        """Commands written in the block, in command form only.

        Descriptive phrases ("very close to TP1", "weekly close") are blanked out first. A command after a negation
        in its clause ("Don't close yet") is left out. A command in a sentence with a condition ("once TP1 is hit",
        "at TP1") or a CLOSE with numbers the bot cannot place ("Close at 23:00") is a problem, and so is a command
        word that is not part of any recognised command ("Closing XAUUSD").
        """
        text = self._blank_descriptions(block)
        cmds = Commands()
        found: dict[FollowUpAction, FollowUp] = {}
        suppressed: set[FollowUpAction] = set()
        covered = cmds.spans
        for rule in self.rules.follow_ups:
            guarded = rule.action not in INFO_ACTIONS
            fu = None
            left_out = False
            for pattern in rule.patterns:
                for m in pattern.finditer(text):
                    covered.append(m.span())
                    if fu is not None:
                        continue
                    problem = self._guard(text, m) if guarded else None
                    if problem:
                        left_out = True
                        cmds.problems.append(problem)
                        continue
                    fu = self._follow_up(rule, m, msg, symbol)
                    if fu is not None and fu.action in CLOSE_ACTIONS and (
                            self._unexplained_numbers(text, m, fu.action)
                            or rule.action is FollowUpAction.CLOSE and FollowUpAction.CLOSE not in suppressed
                            and SIZE_AFTER_CLOSE.search(self._rest_of_sentence(text, m))):
                        left_out = True
                        cmds.problems.append("unclear_close_command")
                        fu = None
            cmds.dropped = cmds.dropped or left_out
            if fu is None:
                if left_out:
                    suppressed |= rule.suppresses  # "don't close half" / "close half at TP1" is never a full close
                continue
            found.setdefault(fu.action, fu)
            if fu.action is rule.action:  # "close 100%" became CLOSE: suppress nothing
                suppressed |= rule.suppresses
        for word in self.rules.command_words:
            for m in word.finditer(text):
                if not any(a < m.end() and m.start() < b for a, b in covered):
                    # "Don't close yet", "Closing XAUUSD", "Gold could break even higher"
                    cmds.problems.append("negated_command" if self._before_in_clause(self.rules.negation, text, m)
                                         else "unclear_command")
        cmds.follow_ups = [fu for action, fu in found.items() if action not in suppressed]
        return cmds

    def _follow_up(self, rule, m: re.Match, msg: IncomingMessage, symbol: str | None) -> FollowUp | None:
        groups = m.groupdict()
        fu = FollowUp(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            action=rule.action,
            reply_to_msg_id=msg.reply_to_msg_id,
            symbol=symbol,
            raw_text=msg.text,
            date=msg.date,
            edited=msg.edited,
        )
        if rule.action is FollowUpAction.CLOSE_PARTIAL:
            pct = groups.get("percent")
            fu.fraction = Decimal(pct) / 100 if pct else rule.default_fraction
            if fu.fraction is not None and fu.fraction >= 1:
                fu.action, fu.fraction = FollowUpAction.CLOSE, None
            elif not fu.fraction or fu.fraction <= 0:
                return None
        if groups.get("price"):
            fu.price = Decimal(groups["price"])
        if groups.get("tp"):
            fu.tp_index = int(groups["tp"])
        return fu

    def _blank_descriptions(self, block: str) -> str:
        for p in self.rules.not_commands:
            block = p.sub(lambda m: " " * len(m.group()), block)  # same length: positions stay valid
        return block

    @staticmethod
    def _blank(block: str, spans: list[tuple[int, int]]) -> str:
        """The block without its commands (and a "now" right after one: "move SL to BE now")."""
        chars = list(block)
        for a, b in spans:
            now = COMMAND_NOW.match(block, b)
            b = now.end() if now else b
            chars[a:b] = [c if c == "\n" else " " for c in chars[a:b]]
        return "".join(chars)

    def _guard(self, text: str, m: re.Match) -> str | None:
        """Why the command matched at `m` must not be carried out now (None: it is an order for now)."""
        if self._before_in_clause(self.rules.negation, text, m):
            return "negated_command"  # "Don't close yet", "Do not cancel your order"
        if self._before_in_clause(self.rules.modal, text, m):
            return "conditional_command"  # "We will move SL to break even"
        if self.rules.condition and self.rules.condition.search(self._sentence(text, m)):
            return "conditional_command"  # "Move SL to BE once TP1 is hit", "Close half at TP1"
        return None

    @staticmethod
    def _before_in_clause(pattern: re.Pattern | None, text: str, m: re.Match) -> bool:
        if pattern is None:
            return False
        start = max((b.end() for b in CLAUSE_END.finditer(text, 0, m.start())), default=0)
        return bool(pattern.search(text, start, m.start()))

    def _sentence(self, text: str, m: re.Match) -> str:
        start = max((b.end() for b in SENTENCE_END.finditer(text, 0, m.start())), default=0)
        end = SENTENCE_END.search(text, m.end())
        return text[start: end.start() if end else len(text)]

    @staticmethod
    def _rest_of_sentence(text: str, m: re.Match) -> str:
        """From the command word to the end of its sentence."""
        end = SENTENCE_END.search(text, m.end())
        return text[m.start(): end.start() if end else len(text)]

    def _unexplained_numbers(self, text: str, m: re.Match, action: FollowUpAction) -> bool:
        """A close whose sentence has numbers besides the close price, the size and P/L notes: "Close at 23:00".

        A whole percent in brackets on a full close ("Close AUDUSD (50%)") may be a size or a P/L: not guessed.
        """
        sentence = self.symbols.remove(self._sentence(text, m))  # "US30", "NAS100"
        if action is FollowUpAction.CLOSE and BRACKET_PERCENT.search(sentence):
            return True
        sentence = CLOSE_PRICE.sub(" ", sentence)
        sentence = self.rules.tp_keyword.sub(" ", sentence)  # "TP1 hit, close now"
        sentence = self._strip_noise(sentence)  # "(+1.43%)", "+80 pips", "50%"
        sentence = CLOSE_SIZE.sub(" ", sentence)  # "1/2", "50 percent"
        return bool(re.search(r"\d", sentence))
