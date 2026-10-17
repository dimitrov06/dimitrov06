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
    maximum_trade_size: Decimal | None = None  # MT5 volume_max


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
    size_distance: Decimal | None = None  # SL distance the size was computed for (>= sl_distance)
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
            # the position closes on the other side of the spread (a buy at the bid): it must be before the SL too
            if (buy and ctx.bid <= s.sl) or (not buy and ctx.ask >= s.sl):
                return RiskDecision.reject("price_beyond_sl")
            if s.tp and ((buy and fill >= s.tp[0]) or (not buy and fill <= s.tp[0])):
                return RiskDecision.reject("price_beyond_tp1")
            ref = s.entry.reference_price
            if ref is not None:
                # Only a move in the trade's favour (towards TP) counts. A move against it, still before the
                # SL, is a better entry with a tighter SL; the size below adapts so the risk stays the same
                # (up to late_entry_min_sl_fraction: beyond that the size stops growing and the risk shrinks).
                best = (s.entry.zone_high if buy else s.entry.zone_low) if s.entry.is_zone else ref
                moved = max(fill - best if buy else best - fill, Decimal(0))
                allowed = abs(ref - s.sl) * cfg.max_entry_slippage_sl_fraction
                if moved > allowed:
                    return RiskDecision.reject(f"price_moved_too_far:{moved}>{allowed}")

        size_distance = sl_distance
        if market and s.entry.reference_price is not None:
            # an adverse late entry with the SL a few points away must not multiply the lot size
            size_distance = max(sl_distance, abs(s.entry.reference_price - s.sl) * cfg.late_entry_min_sl_fraction)

        # a floating loss makes the account smaller: 3% of what is really there
        risk_amount = min(ctx.balance, ctx.nav) * cfg.risk_per_trade_pct / 100
        capped = False
        if cfg.cap_to_daily_budget and ctx.day_start_nav > 0:
            # what is left of today's loss limit (realized + floating loss so far)
            budget_left = max(ctx.day_start_nav * cfg.daily_loss_limit_pct / 100
                              - max(ctx.day_start_nav - ctx.nav, Decimal(0)), Decimal(0))
            capped = budget_left < risk_amount
            risk_amount = min(risk_amount, budget_left)
        loss_per_unit = size_distance * ctx.quote_to_home + cfg.commission_per_lot
        quantum = Decimal(1).scaleb(-ctx.instrument.trade_units_precision)
        units = (risk_amount / loss_per_unit).quantize(quantum, rounding=ROUND_DOWN)
        if units < ctx.instrument.minimum_trade_size:
            return RiskDecision.reject(f"daily_risk_budget:{risk_amount:.2f}" if capped else "position_too_small")
        limits = [x for x in (ctx.instrument.maximum_trade_size, cfg.max_lots.get(s.symbol)) if x is not None]
        if limits and units > min(limits):
            return RiskDecision.reject(f"position_too_large:{units}>{min(limits)}")
        return RiskDecision(
            approved=True, units=units, entry_price=entry_price, sl_distance=sl_distance,
            size_distance=size_distance, risk_amount=risk_amount,
        )


TP_MODES = ("last", "equal")


def plan_parts(units: Decimal, tps: list[Decimal], mode: str, precision: int,
               minimum: Decimal) -> list[tuple[Decimal, Decimal | None]]:
    """Orders to send for one signal as (size, take-profit) pairs.

    "last": one order with the last TP (RTED: TP2 is the real target, TP1 is only an observation level).
    "equal": one order per TP, size split equally.
    """
    if not tps:
        return [(units, None)]
    if mode == "last":
        return [(units, tps[-1])]
    sizes = split_units(units, len(tps), precision, minimum)
    return list(zip(sizes, tps))


def tp_for_part(tps: list[Decimal], part: int, mode: str) -> Decimal | None:
    if not tps:
        return None
    if mode == "last":
        return tps[-1]
    return tps[part] if part < len(tps) else None


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
