"""Loading and compiling the YAML parser rules."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from signal_copier.models import FollowUpAction

DEFAULT_RULES_PATH = Path(__file__).with_name("rules.yaml")


class FollowUpRule(BaseModel):
    action: FollowUpAction
    patterns: list[str]
    default_fraction: Decimal | None = None
    suppresses: list[FollowUpAction] = Field(default_factory=list)


class RulesFile(BaseModel):
    emoji_replacements: dict[str, str] = Field(default_factory=dict)
    symbols: dict[str, str]
    fx_currencies: list[str]
    side: dict[str, list[str]]
    pending_order: dict[str, list[str]]
    market_words: list[str]
    entry: str
    sl: str
    tp_keyword: str
    noise: list[str]
    follow_ups: list[FollowUpRule]


class CompiledFollowUpRule:
    def __init__(self, rule: FollowUpRule):
        self.action = rule.action
        self.patterns = [re.compile(p) for p in rule.patterns]
        self.default_fraction = rule.default_fraction
        self.suppresses = set(rule.suppresses)


class ParserRules:
    """Compiled, ready-to-use version of rules.yaml."""

    def __init__(self, raw: RulesFile):
        self.raw = raw
        self.emoji_replacements = raw.emoji_replacements
        self.side = {s: [re.compile(p) for p in ps] for s, ps in raw.side.items()}
        self.pending_order = {t: [re.compile(p) for p in ps] for t, ps in raw.pending_order.items()}
        self.market_words = [re.compile(p) for p in raw.market_words]
        self.entry = re.compile(raw.entry)
        self.sl = re.compile(raw.sl)
        self.sl_keyword = re.compile(r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\b")
        self.tp_keyword = re.compile(raw.tp_keyword)
        self.noise = [re.compile(p) for p in raw.noise]
        self.follow_ups = [CompiledFollowUpRule(r) for r in raw.follow_ups]

    @classmethod
    def load(cls, path: str | Path | None = None) -> ParserRules:
        with open(path or DEFAULT_RULES_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(RulesFile.model_validate(data))
