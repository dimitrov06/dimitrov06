"""Size and exposure limits with the owner's settings: late adverse entries, lot caps, commission, the daily loss
budget, pending orders after the daily stop, and whether pending orders count towards max_open_positions.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import AUDUSD_SIGNAL, XAUUSD_MARKET, XAUUSD_SELL_LIMIT, bot_positions, make, post, run, sig
from signal_copier.models import Entry, EntryType, Side, Signal
from signal_copier.risk import InstrumentInfo, RiskContext, RiskManager
from signal_copier.settings import RiskCfg

T0 = NOW
XAU = InstrumentInfo("XAUUSD", 2, 2, D("0.01"), maximum_trade_size=D(100))
OWNER = RiskCfg(risk_per_trade_pct=D(3), daily_loss_limit_pct=D(6), max_open_positions=3,
                max_spread={"XAU_USD": D("0.50")})


def gold(ref="4479.69", sl="4470.38"):
    return Signal(chat_id=1, message_id=1, symbol="XAU_USD", raw_symbol="XAUUSD", side=Side.BUY,
                  entry=Entry(type=EntryType.MARKET, price=D(ref)), sl=D(sl), tp=[D("4484.30"), D("4498.30")],
                  raw_text="", date=NOW)


def ctx(**kw):
    base = RiskContext(now=NOW + timedelta(seconds=5), balance=D(10000), nav=D(10000), day_start_nav=D(10000),
                       bid=D("4479.50"), ask=D("4479.80"), quote_to_home=D(100), instrument=XAU,
                       open_positions=0, open_positions_symbol=0)
    return replace(base, **kw)


# ------------------------------------------------- lot size bounds

def test_normal_entry_size():
    d = RiskManager(OWNER).check(gold(), ctx())
    assert d.approved and d.units == D("0.31")  # 300 / (9.42 * 100)


def test_late_adverse_entry_next_to_the_sl_is_capped_at_about_three_times_the_size():
    d = RiskManager(OWNER).check(gold(), ctx(bid=D("4470.85"), ask=D("4471.00")))  # 0.62 above the SL
    assert d.approved and d.units == D("0.97")  # sized on 0.33 x 9.31, not on 0.62 (which would be 4.83 lots)


def test_typo_sl_next_to_the_entry_is_stopped_by_max_lots():
    """SL 4478.38 instead of 4470.38 passes the parser (within 10%): 6.8x the size. risk.max_lots bounds it."""
    assert RiskManager(OWNER).check(gold(sl="4478.38"), ctx()).units == D("2.11")
    capped = RiskManager(OWNER.model_copy(update={"max_lots": {"XAU_USD": D("1.0")}}))
    assert capped.check(gold(sl="4478.38"), ctx()).reason == "position_too_large:2.11>1.0"
    assert capped.check(gold(), ctx()).approved


def test_entry_with_the_sl_inside_the_spread_is_rejected():
    d = RiskManager(OWNER).check(gold(), ctx(bid=D("4470.30"), ask=D("4470.60")))
    assert d.reason == "price_beyond_sl"


def test_size_above_volume_max_or_max_lots_is_rejected():
    small = replace(XAU, maximum_trade_size=D("0.2"))
    assert RiskManager(OWNER).check(gold(), ctx(instrument=small)).reason == "position_too_large:0.31>0.2"
    capped = RiskManager(OWNER.model_copy(update={"max_lots": {"XAU_USD": D("0.25")}}))
    assert capped.check(gold(), ctx()).reason == "position_too_large:0.31>0.25"


def test_commission_is_part_of_the_risk():
    cfg = OWNER.model_copy(update={"commission_per_lot": D("7")})
    d = RiskManager(cfg).check(gold(), ctx())
    assert d.units == D("0.31")  # 300 / (942 + 7) = 0.316
    aud = InstrumentInfo("AUDUSD", 5, 2, D("0.01"))
    s = Signal(chat_id=1, message_id=1, symbol="AUD_USD", raw_symbol="AUDUSD", side=Side.BUY,
               entry=Entry(type=EntryType.MARKET, price=D("0.69401")), sl=D("0.69311"), tp=[D("0.69563")],
               raw_text="", date=NOW)
    c = ctx(instrument=aud, bid=D("0.69400"), ask=D("0.69402"), quote_to_home=D(100000), balance=D(5000),
            nav=D(5000), day_start_nav=D(5000))
    without = RiskManager(OWNER).check(s, c).units
    with_fee = RiskManager(cfg).check(s, c).units
    assert (without, with_fee) == (D("1.64"), D("1.53"))
    assert with_fee * (D("0.00091") * 100000 + 7) <= D(150)  # stop-out incl. commission stays within 3%


def test_retry_after_price_off_does_not_take_a_much_worse_price():
    fake = StrictMt5(demo=False)
    p = make(fake)
    orig = fake.order_send
    calls = {"n": 0}

    def price_off_then_drift(req):
        if req["action"] == fake.TRADE_ACTION_DEAL and "position" not in req and calls["n"] < 3:
            calls["n"] += 1
            fake.sent.append(dict(req))
            bid, ask = fake.ticks["XAUUSD"]
            fake.ticks["XAUUSD"] = (round(bid + 1.5, 2), round(ask + 1.5, 2))  # gold runs during the back-off
            return fake._no(10021, "No prices")
        return orig(req)

    fake.order_send = price_off_then_drift
    post(p, XAUUSD_MARKET, 505, T0)
    assert bot_positions(fake) == []
    assert sig(p, 505)["status"] == "FAILED" and "рискът би бил по-голям" in p.notifier.sent[-1]


def test_retry_of_a_late_entry_is_judged_on_the_distance_its_size_was_computed_for():
    """Late adverse entry 0.62 above the SL, sized as if 3.07 away. After a requote gold is 1.00 higher: the same
    lots now risk 1.62/3.07 of the planned amount, still well inside it, so the retry is sent."""
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4470.85, 4471.00)
    p = make(fake)
    orig = fake.order_send
    calls = {"n": 0}

    def requote_then_move(req):
        if req["action"] == fake.TRADE_ACTION_DEAL and "position" not in req and not calls["n"]:
            calls["n"] += 1
            fake.sent.append(dict(req))
            fake.ticks["XAUUSD"] = (4471.85, 4472.00)
            return fake._no(10004, "Requote")
        return orig(req)

    fake.order_send = requote_then_move
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    assert pos.price_open == 4472.00 and pos.volume == 0.97


def test_retry_after_a_small_requote_still_fills():
    fake = StrictMt5(demo=False)
    fake.script = ["requote"]
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    assert len(bot_positions(fake)) == 1


# ------------------------------------------------- daily loss budget

def test_new_trade_is_sized_to_what_is_left_of_the_daily_limit():
    d = RiskManager(OWNER).check(gold(), ctx(nav=D(9600), balance=D(9600)))  # -4% today: 2% (200) left
    assert d.approved and d.risk_amount == D(200) and d.units == D("0.21")


def test_no_trade_when_almost_nothing_is_left_of_the_daily_limit():
    d = RiskManager(OWNER).check(gold(), ctx(nav=D(9405), balance=D(9405)))  # -5.95%: 5 left, 0.01 lot risks 9.4
    assert d.reason == "daily_risk_budget:5.00"


def test_size_comes_from_equity_when_a_floating_loss_makes_it_smaller_than_the_balance():
    d = RiskManager(OWNER).check(gold(), ctx(balance=D(10000), nav=D(9700)))  # one trade floating -3%
    assert d.risk_amount == D(291) and d.units == D("0.30")  # 3% of 9700, not of 10000
    assert RiskManager(OWNER).check(gold(), ctx(balance=D(10000), nav=D(10300))).risk_amount == D(300)


def test_daily_budget_can_be_switched_off():
    cfg = OWNER.model_copy(update={"cap_to_daily_budget": False})
    assert RiskManager(cfg).check(gold(), ctx(nav=D(9405), balance=D(9405))).units == D("0.29")


def test_daily_stop_cancels_pending_orders():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    assert len(fake.orders) == 1
    fake.equity = 9380.0  # -6.2% today
    run(p.monitor_once())
    assert fake.orders == {}
    assert p.halted_reason() == "daily_loss_limit"
    assert any("отменена чакаща поръчка" in t for t in p.notifier.sent)


def test_daily_stop_retries_a_cancel_that_failed():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)  # records today's start NAV 10000
    fake.equity = 9380.0
    fake.fail = {"order_send": 1, "orders_get": 1}  # the REMOVE gets no answer and the check read fails
    run(p.monitor_once())
    assert len(fake.orders) == 1 and any("не успях да отменя" in t for t in p.notifier.sent)
    run(p.monitor_once())
    assert fake.orders == {}


def test_stop_command_lists_pending_orders():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    reply = run(p.handle_command("/stop"))
    assert "Чакащи поръчки в MT5: 1" in reply and len(fake.orders) == 1


# ------------------------------------------------- owner decision: pending orders and max_open_positions

def _three_limits_then_market(fake, p):
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    for i, px in enumerate(("4156.56", "4157.56", "4158.56")):
        post(p, XAUUSD_SELL_LIMIT.replace("4156.56", px), 800 + i, T0 + timedelta(minutes=i))
    assert len(fake.orders) == 3
    post(p, AUDUSD_SIGNAL, 900, T0 + timedelta(minutes=10))


def test_pending_orders_do_not_count_towards_max_open_by_default():
    fake = StrictMt5(demo=False)
    p = make(fake, max_positions_per_symbol=3)
    assert p.settings.risk.count_pending_in_max_open is False
    _three_limits_then_market(fake, p)
    assert sig(p, 900)["status"] == "EXECUTED"


def test_pending_orders_count_towards_max_open_when_configured():
    fake = StrictMt5(demo=False)
    p = make(fake, max_positions_per_symbol=3, count_pending_in_max_open=True)
    _three_limits_then_market(fake, p)
    assert sig(p, 900)["reason"] == "max_open_positions"


def test_open_positions_still_count_towards_max_open():
    fake = StrictMt5(demo=False)
    p = make(fake, max_open_positions=1)
    post(p, XAUUSD_MARKET, 505, T0)
    post(p, AUDUSD_SIGNAL, 506, T0 + timedelta(minutes=1))
    assert sig(p, 506)["reason"] == "max_open_positions"


def test_pending_orders_still_count_per_symbol():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake, max_positions_per_symbol=1)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    post(p, XAUUSD_SELL_LIMIT.replace("4156.56", "4157.56"), 801, T0 + timedelta(minutes=1))
    assert sig(p, 801)["reason"] == "max_positions_per_symbol"


@pytest.mark.parametrize("include", [True, False])
def test_unknown_orders_always_count(include):
    fake = StrictMt5(demo=False)
    p = make(fake)
    p.storage.create_order("c1", "k1", 0, "XAU_USD", D("0.1"), "LIMIT", D(1), D(1), D(1))
    p.storage.update_order("c1", status="SENT")
    p.storage.create_order("c2", "k2", 0, "XAU_USD", D("0.1"), "LIMIT", D(1), D(1), D(1))
    p.storage.update_order("c2", status="PENDING")
    keys = {r["signal_key"] for r in p.storage.open_positions(include_pending=include)}
    assert keys == ({"k1", "k2"} if include else {"k1"})
