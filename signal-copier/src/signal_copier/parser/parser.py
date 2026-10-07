"""Text -> Signal / FollowUp.

Policy: when in doubt, do not trade. Anything that looks trade-related but
cannot be parsed confidently becomes a ParseIssue (status UNCERTAIN), and
nothing from that message is traded.
"""

from __future__ import annotations

import re
import unicodedata
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
PRICE_OR_ZONE = re.compile(rf"(?P<a>{NUM})(?:[ \t]*(?:-|/|~|\bTO\b)[ \t]*(?P<b>{NUM}))?")
NUMBER = re.compile(NUM)
DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
DROP_CATEGORIES = {"So", "Sk", "Cf", "Cs", "Co", "Mn"}


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
        follow_ups = self._follow_ups(block, msg, symbol.instrument if symbol else None)
        sl = self._find_sl(block)
        tps = self._find_tps(block)

        if len(sides) > 1:
            result.issues.append(ParseIssue(reason="conflicting_sides", text=block))
            return

        if sides:
            if follow_ups and sl is None and not tps:
                # "Gold buy running, move SL to BE" is management, not a new trade
                result.follow_ups.extend(follow_ups)
                return
            if symbol is None:
                result.issues.append(ParseIssue(reason="unknown_symbol", text=block))
                return
            signal_or_issue = self._build_signal(block, msg, symbol, Side(sides.pop()), sl, tps, signal_index)
            if isinstance(signal_or_issue, ParseIssue):
                result.issues.append(signal_or_issue)
            else:
                if follow_ups:
                    signal_or_issue.warnings.append("contains_management_text")
                result.signals.append(signal_or_issue)
            return

        if follow_ups:
            result.follow_ups.extend(follow_ups)
            return

        if sl is not None or tps or self.rules.sl_keyword.search(block):
            result.issues.append(ParseIssue(reason="missing_side", text=block))
        # otherwise: chatter / results post -> ignored

    # ---------------------------------------------------------------- signal

    def _build_signal(
        self, block, msg, symbol, side: Side, sl, tps, index
    ) -> Signal | ParseIssue:
        warnings: list[str] = []
        if isinstance(sl, ParseIssue):
            return sl

        pending = next(
            (t for t, pats in self.rules.pending_order.items() if any(p.search(block) for p in pats)), None
        )
        is_market = any(p.search(block) for p in self.rules.market_words)
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

    def _find_sl(self, block: str) -> Decimal | ParseIssue | None:
        values = {Decimal(m.group("price")) for line in block.splitlines() for m in self.rules.sl.finditer(line)}
        if len(values) > 1:
            return ParseIssue(reason="multiple_sl_values", text=block)
        return values.pop() if values else None

    def _find_tps(self, block: str) -> list[Decimal]:
        tps: list[Decimal] = []
        for line in block.splitlines():
            if not self.rules.tp_keyword.search(line):
                continue
            line = self.rules.sl.sub(" ", line)
            line = self._strip_noise(line)
            for segment in self.rules.tp_keyword.split(line)[1:]:
                for n in NUMBER.findall(segment):
                    value = Decimal(n)
                    if value not in tps:
                        tps.append(value)
        return tps

    def _find_entry(self, block: str) -> tuple[Decimal | None, tuple[Decimal, Decimal] | None]:
        lines = block.splitlines()
        # 1) explicit "ENTRY 2345" / "@ 2345-2350"
        for line in lines:
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

    def _follow_ups(self, block: str, msg: IncomingMessage, symbol: str | None) -> list[FollowUp]:
        found: dict[FollowUpAction, FollowUp] = {}
        suppressed: set[FollowUpAction] = set()
        for rule in self.rules.follow_ups:
            for pattern in rule.patterns:
                m = pattern.search(block)
                if not m:
                    continue
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
                        continue
                if groups.get("price"):
                    fu.price = Decimal(groups["price"])
                if groups.get("tp"):
                    fu.tp_index = int(groups["tp"])
                found.setdefault(fu.action, fu)
                if fu.action is rule.action:  # "close 100%" became CLOSE: suppress nothing
                    suppressed |= rule.suppresses
                break
        return [fu for action, fu in found.items() if action not in suppressed]
