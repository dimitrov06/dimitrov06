"""Channel symbol -> OANDA instrument mapping (XAUUSD/GOLD -> XAU_USD, EURUSD -> EUR_USD)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolMatch:
    raw: str  # as found in the normalized text
    instrument: str  # OANDA name
    start: int
    end: int


class SymbolMapper:
    def __init__(self, aliases: dict[str, str], fx_currencies: list[str]):
        self.aliases = {k.upper(): v for k, v in aliases.items()}
        self.fx = {c.upper() for c in fx_currencies}
        names = sorted(self.aliases, key=len, reverse=True)
        self._alias_re = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None
        self._pair_re = re.compile(r"\b([A-Z]{3})([A-Z]{3})\b")

    def normalize(self, symbol: str) -> str | None:
        """Map a single symbol token; None if unknown."""
        m = self.find(symbol.upper().replace("/", ""))
        return m.instrument if m and m.raw == symbol.upper().replace("/", "") else None

    def find_all(self, text: str) -> list[SymbolMatch]:
        found: list[SymbolMatch] = []
        if self._alias_re:
            for m in self._alias_re.finditer(text):
                found.append(SymbolMatch(m.group(1), self.aliases[m.group(1)], m.start(), m.end()))
        for m in self._pair_re.finditer(text):
            base, quote = m.group(1), m.group(2)
            if base in self.fx and quote in self.fx and base != quote:
                if not any(f.start == m.start() for f in found):
                    found.append(SymbolMatch(m.group(0), f"{base}_{quote}", m.start(), m.end()))
        return sorted(found, key=lambda s: s.start)

    def find(self, text: str) -> SymbolMatch | None:
        found = self.find_all(text)
        return found[0] if found else None

    def remove(self, text: str) -> str:
        for m in reversed(self.find_all(text)):
            text = text[: m.start] + " " + text[m.end :]
        return text
