"""Loading and compiling the YAML parser rules."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from signal_copier.models import FollowUpAction
from signal_copier.settings import ConfigError

DEFAULT_RULES_PATH = Path(__file__).with_name("rules.yaml")
MACRO = re.compile(r"\{([A-Z][A-Z_]*)\}")
# sections a rules file must have: without them chatter becomes commands and updates become new trades
SAFETY_KEYS = ("unclear", "macros", "pending_hint", "order_word", "not_commands", "command_guards", "command_words",
               "results", "update_words")
GUARD_KEYS = ("negation", "modal", "condition")


class FollowUpRule(BaseModel):
    action: FollowUpAction
    patterns: list[str]
    default_fraction: Decimal | None = None
    suppresses: list[FollowUpAction] = Field(default_factory=list)


class CommandGuards(BaseModel):
    """A command caught by one of these is not carried out (UNCERTAIN; inside a signal post: left out)."""

    negation: str | None = None  # before the command in its clause: "Don't close yet"
    modal: str | None = None  # before the command in its clause: "We will move SL to BE"
    condition: str | None = None  # in the command's sentence: "once TP1 is hit", "close half at TP1"


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
    unclear: list[str] = Field(default_factory=list)  # a command with these is never acted on (UNCERTAIN)
    # load() refuses a file without the protections below (see SAFETY_KEYS); tests may build rules without them
    macros: dict[str, str] = Field(default_factory=dict)  # {NAME} in command patterns; {SYMBOL} is built in
    pending_hint: str | None = None  # an order type pending_order does not recognise -> UNCERTAIN, never market
    order_word: str | None = None
    not_commands: list[str] = Field(default_factory=list)  # blanked out before commands are looked for
    command_guards: CommandGuards = Field(default_factory=CommandGuards)
    command_words: list[str] = Field(default_factory=list)  # outside a recognised command -> UNCERTAIN
    results: list[str] = Field(default_factory=list)  # "Closed", "Cancelled": reports, not a new trade
    update_words: list[str] = Field(default_factory=list)  # "update", "running", "moved": not a new trade


class CompiledFollowUpRule:
    def __init__(self, rule: FollowUpRule, compile_command):
        self.action = rule.action
        self.patterns = [compile_command(p) for p in rule.patterns]
        self.default_fraction = rule.default_fraction
        self.suppresses = set(rule.suppresses)


class ParserRules:
    """Compiled, ready-to-use version of rules.yaml."""

    def __init__(self, raw: RulesFile):
        self.raw = raw
        self.macros = {**raw.macros, "SYMBOL": self._symbol_pattern(raw)}
        self.emoji_replacements = raw.emoji_replacements
        self.side = {s: [re.compile(p) for p in ps] for s, ps in raw.side.items()}
        self.pending_order = {t: [re.compile(p) for p in ps] for t, ps in raw.pending_order.items()}
        self.pending_hint = re.compile(raw.pending_hint) if raw.pending_hint else None
        self.order_word = re.compile(raw.order_word) if raw.order_word else None
        self.market_words = [re.compile(p) for p in raw.market_words]
        self.entry = re.compile(raw.entry)
        self.sl = re.compile(raw.sl)
        self.sl_keyword = re.compile(r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\b")
        self.tp_keyword = re.compile(raw.tp_keyword)
        self.noise = [re.compile(p) for p in raw.noise]
        self.follow_ups = [CompiledFollowUpRule(r, self.compile_command) for r in raw.follow_ups]
        self.unclear = [self.compile_command(p) for p in raw.unclear]
        self.not_commands = [self.compile_command(p) for p in raw.not_commands]
        guards = raw.command_guards
        self.negation = self.compile_command(guards.negation) if guards.negation else None
        self.modal = self.compile_command(guards.modal) if guards.modal else None
        self.condition = self.compile_command(guards.condition) if guards.condition else None
        self.command_words = [self.compile_command(p) for p in raw.command_words]
        self.results = [self.compile_command(p) for p in raw.results]
        self.update_words = [self.compile_command(p) for p in raw.update_words]
        self.tp_hit = re.compile(r"\b(?:HIT|REACHED|DONE|SMASHED|CLOSED|SECURED|BOOKED)\b")

    @staticmethod
    def _symbol_pattern(raw: RulesFile) -> str:
        """Any alias, or a pair of two fx_currencies ("XAUUSD", "GOLD", "AUDUSD")."""
        names = sorted((re.escape(a.upper()) for a in raw.symbols), key=len, reverse=True)
        fx = "|".join(sorted(re.escape(c.upper()) for c in raw.fx_currencies))
        alternatives = names + ([f"(?:{fx})(?:{fx})"] if fx else [])
        return r"\b(?:" + "|".join(alternatives) + r")\b" if alternatives else r"(?!)"

    def expand(self, pattern: str) -> str:
        """Fill in {NAME} macros (nested ones too); an unknown {NAME} is a mistake in the rules file."""
        for _ in range(10):
            expanded = MACRO.sub(lambda m: self._macro(m.group(1), pattern), pattern)
            if expanded == pattern:
                return expanded
            pattern = expanded
        raise ValueError(f"macros nested too deep in rules pattern: {pattern[:80]}")

    def _macro(self, name: str, pattern: str) -> str:
        if name not in self.macros:
            raise ValueError(f"unknown macro {{{name}}} in rules pattern: {pattern[:80]}")
        return self.macros[name]

    def compile_command(self, pattern: str) -> re.Pattern:
        return re.compile(self.expand(pattern), re.MULTILINE)

    @classmethod
    def load(cls, path: str | Path | None = None) -> ParserRules:
        """A file that cannot be used (missing, not YAML, a bad regex, an older version) is a ConfigError: the bot
        stops and tells the owner instead of restarting forever without a word."""
        where = path or DEFAULT_RULES_PATH
        try:
            with open(where, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"parser rules {where} cannot be read: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"parser rules {where} is not a YAML mapping of rules")
        missing = [k for k in SAFETY_KEYS if not data.get(k)]
        missing += [f"command_guards.{k}" for k in GUARD_KEYS if not (data.get("command_guards") or {}).get(k)]
        if missing:
            # an older custom rules_file would take "very close to TP1" for a CLOSE: never trade with it
            raise ConfigError(
                f"parser rules {path or DEFAULT_RULES_PATH} lack {', '.join(missing)}: the file is older than this "
                "version. Copy the built-in src/signal_copier/parser/rules.yaml and add your changes to it again."
            )
        try:
            return cls(RulesFile.model_validate(data))
        except (ValueError, TypeError, AttributeError, re.error) as e:  # pydantic errors are ValueErrors
            raise ConfigError(f"parser rules {where} are invalid: {e}") from e
