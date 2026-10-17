"""An order whose answer was lost stays SENT ("unknown") and is never resent. The monitor gives it up only after
MT5 has shown no trace of it (positions, pending orders, opening deals; every read answered, terminal connected)
for executor.unknown_order_expiry_minutes in a row: FAILED, its slot is free, the owner is told. A failed read
or a disconnected terminal never expires it, and an order that turns up later is adopted.
"""

from datetime import timedelta

import pytest

from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import AUDUSD_SIGNAL, XAUUSD_MARKET, bot_positions, make, post, run, sig

T0 = NOW
KEY = "-1001:505:0"


def lost_then_down(fake):
    """The next order_send executes, then the terminal's IPC drops (answer lost, the tag polls all fail)."""
    orig = fake.order_send

    def send(req):
        orig(req)
        fake.order_send = orig
        fake.ipc_down = True
        return None

    fake.order_send = send


def unknown_gold(**risk):
    """XAUUSD buy whose order_send never reached the server and answered None: MT5 has nothing."""
    fake = StrictMt5(demo=False)
    fake.script = ["none"]
    p = make(fake, **risk)
    clock = {"t": T0}
    p.executor.clock = lambda: clock["t"]
    post(p, XAUUSD_MARKET, 505, T0)
    assert [o["status"] for o in p.storage.orders_for_signal(KEY)] == ["SENT"] and fake.positions == {}
    return fake, p, clock


def monitor_at(p, clock, minutes):
    clock["t"] = T0 + timedelta(minutes=minutes)
    run(p.monitor_once())


def statuses(p):
    return [o["status"] for o in p.storage.orders_for_signal(KEY)]


def test_unknown_order_with_no_trace_expires_after_ten_minutes_and_frees_its_slot():
    fake, p, clock = unknown_gold(max_open_positions=1)
    assert len(p.storage.open_positions(include_pending=False)) == 1  # the unknown order holds the slot
    monitor_at(p, clock, 1)  # first round without a trace
    monitor_at(p, clock, 6)
    assert statuses(p) == ["SENT"]
    monitor_at(p, clock, 11)  # 10 min without a trace
    assert statuses(p) == ["FAILED"] and sig(p, 505)["status"] == "FAILED"
    assert p.storage.open_positions() == []
    assert any(t.startswith("⌛ Поръчката без отговор XAU_USD") and "НЕ е изпълнена" in t for t in p.notifier.sent)
    post(p, AUDUSD_SIGNAL, 506, T0 + timedelta(minutes=12))  # the slot is free again
    assert sig(p, 506)["status"] == "EXECUTED" and len(bot_positions(fake)) == 1


def test_expiry_time_is_configurable():
    fake, p, clock = unknown_gold()
    p.executor.unknown_expiry = timedelta(minutes=30)
    monitor_at(p, clock, 1)
    monitor_at(p, clock, 12)
    assert statuses(p) == ["SENT"]
    monitor_at(p, clock, 31)
    assert statuses(p) == ["FAILED"]


def test_default_expiry_is_ten_minutes():
    from signal_copier.settings import ExecutorCfg

    assert ExecutorCfg().unknown_order_expiry_minutes == 10


@pytest.mark.parametrize("read", ["positions_get", "orders_get", "history_deals_get", "terminal_info"])
def test_failed_read_never_expires_it_and_starts_the_wait_again(read):
    fake, p, clock = unknown_gold()
    monitor_at(p, clock, 1)
    fake.fail = {read: 1}  # MT5 does not answer this one read in the round that would expire it
    monitor_at(p, clock, 11)
    assert statuses(p) == ["SENT"]
    monitor_at(p, clock, 12)  # the wait starts again here
    monitor_at(p, clock, 21)
    assert statuses(p) == ["SENT"]
    monitor_at(p, clock, 22)
    assert statuses(p) == ["FAILED"]


def test_disconnected_terminal_never_expires_it():
    """Without the broker connection the terminal's lists can be stale: absence there proves nothing."""
    fake, p, clock = unknown_gold()
    monitor_at(p, clock, 1)
    fake.connected = False
    monitor_at(p, clock, 11)
    monitor_at(p, clock, 30)
    assert statuses(p) == ["SENT"]
    fake.connected = True
    monitor_at(p, clock, 31)
    monitor_at(p, clock, 40)
    assert statuses(p) == ["SENT"]
    monitor_at(p, clock, 41)
    assert statuses(p) == ["FAILED"]


def test_lost_answer_filled_and_closed_before_the_monitor_is_adopted_not_expired():
    fake = StrictMt5(demo=False)
    lost_then_down(fake)
    p = make(fake)
    clock = {"t": T0}
    p.executor.clock = lambda: clock["t"]
    post(p, XAUUSD_MARKET, 505, T0)
    assert statuses(p) == ["SENT"]
    fake.ipc_down = False
    (ticket,) = fake.positions
    fake.hit_tp(ticket, 4498.30)  # gone from positions; only its deals carry the tag
    monitor_at(p, clock, 11)
    assert statuses(p) == ["FILLED"] and sig(p, 505)["status"] == "EXECUTED"
    (trade,) = p.storage.trades_for_signal(KEY, open_only=False)
    assert trade["state"] == "CLOSED" and trade["realized_pl"] is not None
    assert any("е била изпълнена в MT5 и вече е затворена" in t for t in p.notifier.sent)
    assert any(t.startswith("🏁 Затворена XAU_USD") for t in p.notifier.sent)
    assert not any(t.startswith("⌛") for t in p.notifier.sent)


def test_lost_answer_that_is_a_live_position_is_adopted_not_expired():
    fake = StrictMt5(demo=False)
    lost_then_down(fake)
    p = make(fake)
    clock = {"t": T0}
    p.executor.clock = lambda: clock["t"]
    post(p, XAUUSD_MARKET, 505, T0)
    fake.ipc_down = False
    monitor_at(p, clock, 30)
    assert statuses(p) == ["FILLED"] and len(p.storage.trades_for_signal(KEY)) == 1


def test_order_given_up_that_turns_up_later_is_adopted():
    fake, p, clock = unknown_gold()
    monitor_at(p, clock, 1)
    monitor_at(p, clock, 11)
    assert statuses(p) == ["FAILED"]
    # it did execute after all (the terminal showed it late): same tag, the bot's magic
    (sent,) = [r for r in fake.sent if r["action"] == fake.TRADE_ACTION_DEAL]
    fake._execute(sent)
    monitor_at(p, clock, 12)
    assert statuses(p) == ["FILLED"] and sig(p, 505)["status"] == "EXECUTED"
    assert len(p.storage.trades_for_signal(KEY)) == 1
    assert any("Поръчката без отговор е изпълнена в MT5" in t for t in p.notifier.sent)


def test_unknown_order_is_never_resent_after_it_expired():
    fake, p, clock = unknown_gold()
    monitor_at(p, clock, 1)
    monitor_at(p, clock, 11)
    sends = len(fake.market_sends())
    from signal_copier.models import IncomingMessage

    run(p.handle_message(IncomingMessage(chat_id=-1001, message_id=505, text=XAUUSD_MARKET + " ", date=T0,
                                         edited=True)))
    assert len(fake.market_sends()) == sends
