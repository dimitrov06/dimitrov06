from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from conftest import NOW
from signal_copier.models import Entry, EntryType, Side, Signal
from signal_copier.risk import InstrumentInfo, RiskContext, RiskManager, split_units
from signal_copier.settings import RiskCfg

EURUSD = InstrumentInfo("EUR_USD", 5, 0, D(1))


def sig(side=Side.BUY, price="1.10000", sl="1.09500", tp=("1.11000",), entry_type=EntryType.MARKET, zone=None):
    entry = Entry(type=entry_type, zone_low=D(zone[0]), zone_high=D(zone[1])) if zone else \
        Entry(type=entry_type, price=D(price) if price else None)
    return Signal(chat_id=1, message_id=1, symbol="EUR_USD", raw_symbol="EURUSD", side=side, entry=entry,
                  sl=D(sl) if sl else None, tp=[D(t) for t in tp], raw_text="", date=NOW)


def ctx(**kw):
    base = RiskContext(now=NOW + timedelta(seconds=5), balance=D(10000), nav=D(10000), day_start_nav=D(10000),
                       bid=D("1.09999"), ask=D("1.10000"), quote_to_home=D(1), instrument=EURUSD,
                       open_positions=0, open_positions_symbol=0)
    return replace(base, **kw)


rm = RiskManager(RiskCfg())


def test_size_from_fixed_percent_risk():
    d = rm.check(sig(), ctx())
    # 0.5% of 10000 = 50; SL distance 0.005 -> 10000 units
    assert d.approved and d.units == D(10000) and d.risk_amount == D(50)


def test_size_uses_quote_to_account_conversion():
    d = rm.check(sig(), ctx(quote_to_home=D("0.5")))
    assert d.units == D(20000)


def test_size_rounds_down():
    d = rm.check(sig(sl="1.09700"), ctx())  # 50 / 0.003 = 16666.6
    assert d.units == D(16666)


@pytest.mark.parametrize("signal, context, reason", [
    (sig(sl=None), ctx(), "missing_sl"),
    (sig(), ctx(duplicate=True), "duplicate"),
    (sig(), ctx(halted_reason="manual_stop"), "halted:manual_stop"),
    (sig(), ctx(now=NOW + timedelta(seconds=120)), "signal_too_old"),
    (sig(), ctx(open_positions=5), "max_open_positions"),
    (sig(), ctx(open_positions_symbol=2), "max_positions_per_symbol"),
    (sig(), ctx(bid=D("1.09900"), ask=D("1.10000")), "spread_too_wide"),
    (sig(), ctx(bid=D("1.09799"), ask=D("1.09800")), "price_moved_too_far"),
    (sig(), ctx(bid=D("1.09400"), ask=D("1.09401")), "price_beyond_sl"),
    (sig(), ctx(bid=D("1.11100"), ask=D("1.11101")), "price_beyond_tp1"),
    (sig(), ctx(nav=D(9790)), "daily_loss_limit"),
    (sig(), ctx(balance=D("0.5")), "position_too_small"),
])
def test_rejections(signal, context, reason):
    d = rm.check(signal, context)
    assert not d.approved and d.reason.startswith(reason)


def test_daily_loss_sets_halt_flag():
    assert rm.check(sig(), ctx(nav=D(9790))).halt


def test_sell_slippage_and_sl_side():
    s = sig(side=Side.SELL, sl="1.10500", tp=("1.09000",))
    assert rm.check(s, ctx()).approved
    assert rm.check(s, ctx(bid=D("1.10600"), ask=D("1.10601"))).reason == "price_beyond_sl"


def test_zone_entry_inside_zone_is_fine():
    s = sig(price=None, zone=("1.09950", "1.10100"))
    assert rm.check(s, ctx()).approved


def test_limit_order_sizes_from_limit_price_not_market():
    s = sig(price="1.09800", sl="1.09300", entry_type=EntryType.LIMIT)
    d = rm.check(s, ctx())
    assert d.approved and d.entry_price == D("1.09800") and d.units == D(10000)


def test_configurable_max_spread_per_symbol():
    strict = RiskManager(RiskCfg(max_spread={"EUR_USD": D("0.00001")}))
    assert strict.check(sig(), ctx(bid=D("1.09998"))).reason.startswith("spread_too_wide")


def test_split_units():
    assert split_units(D(10), 2, 0, D(1)) == [D(5), D(5)]
    assert split_units(D(11), 3, 0, D(1)) == [D(3), D(3), D(5)]
    assert split_units(D(1), 2, 0, D(1)) == [D(1)]  # too small to split
