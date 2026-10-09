"""Pre-trade checks and position sizing. Pure logic: all market data comes in via RiskContext."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from signal_copier.models import EntryType, Side, Signal
from signal_copier.settings import RiskCfg


@dataclass(frozen=True)
class InstrumentInfo:
    name: str
    display_precision: int  # decimals in prices
    trade_units_precision: int  # decimals in units (0 for FX)
    minimum_trade_size: Decimal
    pip_location: int = -4


@dataclass
class RiskContext:
    now: datetime
    balance: Decimal
    nav: Decimal
    day_start_nav: Decimal
    bid: Decimal
    ask: Decimal
    quote_to_home: Decimal  # value of 1.0 quote currency in account currency
    instrument: InstrumentInfo
    open_positions: int  # signals with open trades/pending orders
    open_positions_symbol: int
    duplicate: bool = False
    halted_reason: str | None = None  # /stop or earlier daily-limit halt


@dataclass
class RiskDecision:
    approved: bool
    reason: str | None = None
    units: Decimal = Decimal(0)  # absolute size; sign comes from side
    entry_price: Decimal | None = None
    sl_distance: Decimal | None = None
    risk_amount: Decimal | None = None
    halt: bool = False  # daily loss limit reached -> stop trading until tomorrow

    @classmethod
    def reject(cls, reason: str, **kw) -> RiskDecision:
        return cls(approved=False, reason=reason, **kw)


class RiskManager:
    def __init__(self, cfg: RiskCfg):
        self.cfg = cfg

    def daily_loss_hit(self, nav: Decimal, day_start_nav: Decimal) -> bool:
        if day_start_nav <= 0:
            return False
        loss_pct = (day_start_nav - nav) / day_start_nav * 100
        return loss_pct >= self.cfg.daily_loss_limit_pct

    def check(self, s: Signal, ctx: RiskContext) -> RiskDecision:
        cfg = self.cfg
        if ctx.duplicate:
            return RiskDecision.reject("duplicate")
        if ctx.halted_reason:
            return RiskDecision.reject(f"halted:{ctx.halted_reason}")
        if self.daily_loss_hit(ctx.nav, ctx.day_start_nav):
            return RiskDecision.reject("daily_loss_limit", halt=True)
        if s.sl is None:
            return RiskDecision.reject("missing_sl")
        age = (ctx.now - s.date).total_seconds()
        if age > cfg.max_signal_age_seconds:
            return RiskDecision.reject(f"signal_too_old:{int(age)}s")
        if ctx.open_positions >= cfg.max_open_positions:
            return RiskDecision.reject("max_open_positions")
        if ctx.open_positions_symbol >= cfg.max_positions_per_symbol:
            return RiskDecision.reject("max_positions_per_symbol")

        buy = s.side is Side.BUY
        market = s.entry.type is EntryType.MARKET
        fill = ctx.ask if buy else ctx.bid
        entry_price = fill if market else s.entry.reference_price
        sl_distance = abs(entry_price - s.sl)
        if sl_distance == 0:
            return RiskDecision.reject("zero_sl_distance")

        spread = ctx.ask - ctx.bid
        max_spread = cfg.max_spread.get(s.symbol)
        if max_spread is None:
            max_spread = sl_distance * cfg.default_max_spread_sl_fraction
        if spread > max_spread:
            return RiskDecision.reject(f"spread_too_wide:{spread}>{max_spread}")

        if market:
            if (buy and fill <= s.sl) or (not buy and fill >= s.sl):
                return RiskDecision.reject("price_beyond_sl")
            if s.tp and ((buy and fill >= s.tp[0]) or (not buy and fill <= s.tp[0])):
                return RiskDecision.reject("price_beyond_tp1")
            ref = s.entry.reference_price
            if ref is not None:
                if s.entry.is_zone and s.entry.zone_low <= fill <= s.entry.zone_high:
                    moved = Decimal(0)
                elif s.entry.is_zone:
                    moved = min(abs(fill - s.entry.zone_low), abs(fill - s.entry.zone_high))
                else:
                    moved = abs(fill - ref)
                allowed = abs(ref - s.sl) * cfg.max_entry_slippage_sl_fraction
                if moved > allowed:
                    return RiskDecision.reject(f"price_moved_too_far:{moved}>{allowed}")

        risk_amount = ctx.balance * cfg.risk_per_trade_pct / 100
        loss_per_unit = sl_distance * ctx.quote_to_home
        quantum = Decimal(1).scaleb(-ctx.instrument.trade_units_precision)
        units = (risk_amount / loss_per_unit).quantize(quantum, rounding=ROUND_DOWN)
        if units < ctx.instrument.minimum_trade_size:
            return RiskDecision.reject("position_too_small")
        return RiskDecision(
            approved=True, units=units, entry_price=entry_price, sl_distance=sl_distance, risk_amount=risk_amount
        )


def split_units(units: Decimal, parts: int, precision: int, minimum: Decimal) -> list[Decimal]:
    """Split a position over N take-profits. Uses fewer parts if a part would be below the minimum size."""
    quantum = Decimal(1).scaleb(-precision)
    parts = max(parts, 1)
    while parts > 1:
        base = (units / parts).quantize(quantum, rounding=ROUND_DOWN)
        if base >= minimum:
            break
        parts -= 1
    base = (units / parts).quantize(quantum, rounding=ROUND_DOWN)
    sizes = [base] * parts
    sizes[-1] += units - base * parts
    return sizes
