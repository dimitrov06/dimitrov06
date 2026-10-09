"""OANDA executor against the fake API: order bodies, split by TP, and the never-double-order rule."""

import asyncio
from decimal import Decimal as D

from conftest import NOW, _no_sleep
from signal_copier.executor import Executor, OandaClient
from signal_copier.models import Entry, EntryType, Side, Signal
from signal_copier.risk import InstrumentInfo, RiskDecision
from signal_copier.settings import OandaCredentials
from signal_copier.storage import Storage

INFO = InstrumentInfo("XAU_USD", display_precision=3, trade_units_precision=0, minimum_trade_size=D(1))


def run(coro):
    return asyncio.run(coro)


def setup(fake, attempts=3):
    storage = Storage(":memory:")
    client = OandaClient(OandaCredentials(base_url="https://fake", token="t", account_id="ACC"),
                         retry_attempts=attempts, retry_base_delay_s=0, transport=fake.transport(), sleep=_no_sleep)
    return storage, Executor(client, storage, retry_attempts=attempts, retry_base_delay_s=0)


def gold(side=Side.BUY, entry=None, tp=("4484.30", "4498.30"), sl="4470.38", message_id=900):
    return Signal(chat_id=-1001, message_id=message_id, symbol="XAU_USD", raw_symbol="XAUUSD", side=side,
                  entry=entry or Entry(type=EntryType.MARKET, price=D("4479.69")), sl=D(sl),
                  tp=[D(t) for t in tp], raw_text="x", date=NOW)


DECISION = RiskDecision(approved=True, units=D(5))


def test_market_order_split_and_bodies(fake):
    storage, ex = setup(fake)
    r = run(ex.open_signal(gold(), DECISION, INFO))
    assert len(r.opened) == 2 and not r.errors
    bodies = [b["order"] for _, _, b in fake.market_orders()]
    assert [b["units"] for b in bodies] == ["2", "3"]
    assert bodies[0]["clientExtensions"]["id"] == "sc_n1001_900_0_0"
    assert bodies[0]["stopLossOnFill"]["price"] == "4470.380"


def test_sell_units_are_negative(fake):
    storage, ex = setup(fake)
    run(ex.open_signal(gold(Side.SELL, tp=("4470",), sl="4490"), DECISION, INFO))
    assert fake.market_orders()[0][2]["order"]["units"] == "-5"


def test_lost_response_is_looked_up_not_resent(fake):
    storage, ex = setup(fake)
    fake.post_failures = ["after"]  # OANDA filled part 0, but our response timed out
    r = run(ex.open_signal(gold(), DECISION, INFO))
    assert len(fake.orders) == 2  # exactly one order per part at OANDA
    assert len(fake.market_orders()) == 2
    assert len(r.opened) == 2 and not r.errors
    assert storage.get_order("sc_n1001_900_0_0")["status"] == "FILLED"


def test_timeout_before_reaching_oanda_is_retried_once(fake):
    storage, ex = setup(fake)
    fake.post_failures = ["before"]
    r = run(ex.open_signal(gold(), DECISION, INFO))
    assert len(fake.orders) == 2 and len(r.opened) == 2
    assert len(fake.market_orders()) == 3  # 1 failed attempt + 2 successful


def test_gives_up_after_retries_without_duplicates(fake):
    storage, ex = setup(fake, attempts=2)
    fake.post_failures = ["before"] * 10
    r = run(ex.open_signal(gold(tp=("4490",)), DECISION, INFO))
    assert r.errors and not r.opened
    assert storage.get_order("sc_n1001_900_0_0")["status"] == "FAILED"
    fake.post_failures = []
    r2 = run(ex.open_signal(gold(tp=("4490",)), DECISION, INFO))  # same signal again: never resent automatically
    assert r2.skipped and fake.orders == {}


def test_running_same_signal_twice_does_not_double(fake):
    storage, ex = setup(fake)
    run(ex.open_signal(gold(), DECISION, INFO))
    r = run(ex.open_signal(gold(), DECISION, INFO))
    assert len(r.skipped) == 2 and len(fake.market_orders()) == 2


def test_crash_after_insert_before_send_is_looked_up(fake):
    storage, ex = setup(fake)
    s = gold(tp=("4490",))
    storage.create_order("sc_n1001_900_0_0", s.key, 0, "XAU_USD", D(5), "MARKET", None, s.sl, D("4490"))
    run(ex.open_signal(s, DECISION, INFO))  # not at OANDA -> sent now
    assert len(fake.orders) == 1


def test_oanda_rejection_is_recorded(fake):
    fake.handle_orig = fake.handle

    def reject(req):
        if req.method == "POST":
            import httpx

            return httpx.Response(400, json={"errorCode": "MARKET_HALTED"})
        return fake.handle_orig(req)

    fake.handle = reject
    storage, ex = setup(fake)  # transport binds fake.handle at creation
    r = run(ex.open_signal(gold(tp=("4490",)), DECISION, INFO))
    assert "MARKET_HALTED" in r.errors[0]
    assert storage.get_order("sc_n1001_900_0_0")["status"] == "REJECTED"


def test_limit_order_then_fill_detected_by_sync(fake):
    storage, ex = setup(fake)
    s = gold(entry=Entry(type=EntryType.LIMIT, price=D("4475.00")), tp=("4490",))
    r = run(ex.open_signal(s, DECISION, INFO))
    body = fake.market_orders()[0][2]["order"]
    assert body["type"] == "LIMIT" and body["price"] == "4475.000" and body["timeInForce"] == "GTD"
    assert len(r.pending) == 1

    fake.fill_pending("sc_n1001_900_0_0")
    opened, closed = run(ex.sync())
    assert len(opened) == 1 and storage.trades_for_signal(s.key)

    tid = opened[0]["trade_id"]
    fake.close(tid, "4490.000", "75.0")
    opened, closed = run(ex.sync())
    assert closed[0]["realized_pl"] == "75.0"
    assert storage.open_trades() == []


def test_cancel_pending(fake):
    from signal_copier.models import FollowUp, FollowUpAction

    storage, ex = setup(fake)
    s = gold(entry=Entry(type=EntryType.LIMIT, price=D("4475.00")), tp=("4490",))
    run(ex.open_signal(s, DECISION, INFO))
    fu = FollowUp(chat_id=-1001, message_id=901, action=FollowUpAction.CANCEL, reply_to_msg_id=900,
                  raw_text="cancel", date=NOW)
    done = run(ex.apply_follow_up(fu, s.key, INFO))
    assert done and fake.orders["sc_n1001_900_0_0"]["state"] == "CANCELLED"
