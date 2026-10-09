"""Domain models shared by all modules (parser, risk, executor, storage)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class EntryType(str, Enum):
    MARKET = "MARKET"  # open now; price/zone (if any) is only a reference
    LIMIT = "LIMIT"
    STOP = "STOP"


class Entry(BaseModel):
    type: EntryType = EntryType.MARKET
    price: Decimal | None = None  # single price
    zone_low: Decimal | None = None  # zone "2345-2350"
    zone_high: Decimal | None = None

    @model_validator(mode="after")
    def _check(self) -> Entry:
        if (self.zone_low is None) != (self.zone_high is None):
            raise ValueError("zone needs both low and high")
        if self.zone_low is not None and self.zone_low > self.zone_high:
            raise ValueError("zone_low > zone_high")
        if self.type is not EntryType.MARKET and self.reference_price is None:
            raise ValueError(f"{self.type.value} entry needs a price")
        return self

    @property
    def is_zone(self) -> bool:
        return self.zone_low is not None

    @property
    def reference_price(self) -> Decimal | None:
        """Price used for SL distance / slippage checks."""
        if self.price is not None:
            return self.price
        if self.zone_low is not None:
            return (self.zone_low + self.zone_high) / 2
        return None


class IncomingMessage(BaseModel):
    """A Telegram message as handed from listener/ to parser/."""

    chat_id: int
    message_id: int
    text: str
    date: datetime
    reply_to_msg_id: int | None = None
    edited: bool = False


class Signal(BaseModel):
    chat_id: int
    message_id: int
    index: int = 0  # position when one message contains several signals
    symbol: str  # OANDA instrument, e.g. XAU_USD
    raw_symbol: str  # as written in the channel, e.g. GOLD
    side: Side
    entry: Entry
    sl: Decimal | None = None  # None => risk/ rejects it
    tp: list[Decimal] = Field(default_factory=list)
    raw_text: str
    date: datetime
    edited: bool = False
    warnings: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        """Dedup key: one signal per (chat, message, index)."""
        return f"{self.chat_id}:{self.message_id}:{self.index}"


class FollowUpAction(str, Enum):
    MOVE_SL_BE = "MOVE_SL_BE"
    MOVE_SL = "MOVE_SL"  # to an explicit price
    CLOSE = "CLOSE"
    CLOSE_PARTIAL = "CLOSE_PARTIAL"
    TP_HIT = "TP_HIT"  # informational; executor verifies state
    SL_HIT = "SL_HIT"  # informational
    CANCEL = "CANCEL"  # cancel pending orders


class FollowUp(BaseModel):
    chat_id: int
    message_id: int
    action: FollowUpAction
    reply_to_msg_id: int | None = None  # primary way to find the target signal
    symbol: str | None = None  # fallback when the group writes "GOLD close"
    fraction: Decimal | None = None  # CLOSE_PARTIAL, e.g. 0.5
    tp_index: int | None = None  # TP_HIT, 1-based
    price: Decimal | None = None  # MOVE_SL target, or price the channel quotes for CLOSE
    raw_text: str
    date: datetime
    edited: bool = False

    @property
    def has_target(self) -> bool:
        return self.reply_to_msg_id is not None or self.symbol is not None


class ParseIssue(BaseModel):
    """A part of a message that looked trade-related but was not parsed confidently."""

    reason: str
    text: str


class ParseStatus(str, Enum):
    SIGNAL = "SIGNAL"
    FOLLOW_UP = "FOLLOW_UP"
    UNCERTAIN = "UNCERTAIN"  # do not trade, store + notify
    IGNORED = "IGNORED"  # chatter, ads, results posts


class ParseResult(BaseModel):
    message: IncomingMessage
    signals: list[Signal] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)
    issues: list[ParseIssue] = Field(default_factory=list)

    @property
    def status(self) -> ParseStatus:
        if self.issues:
            return ParseStatus.UNCERTAIN
        if self.signals:
            return ParseStatus.SIGNAL
        if self.follow_ups:
            return ParseStatus.FOLLOW_UP
        return ParseStatus.IGNORED
