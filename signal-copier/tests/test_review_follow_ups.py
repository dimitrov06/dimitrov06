"""Final review of today's changes: follow-ups and edits on the owner's real-money setup (fake MT5).

- a command retried after FAILED/REJECTED (the channel edits it) acts on the trade it was about, never on a newer one
  the symbol finds now (retry-re-resolves-target);
- an edit that drops the symbol from a partial close does not close another part (partial-close-key-change);
- after one edit that added a command, later level edits still work (raw-text-never-updated);
- an edit that adds command-like text the parser leaves out is reported (P4-edit-with-dropped-command-silent).
"""

from datetime import timedelta

import pytest

from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import XAUUSD_CLOSE, XAUUSD_MARKET, XAUUSD_SELL_LIMIT, bot_positions, edit, events, make, post, run

T0 = NOW


# ------------------------------------------------- retry-re-resolves-target

def test_failed_close_retried_by_an_edit_never_closes_a_newer_trade():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)  # trade A
    (a,) = bot_positions(fake)
    fake.fail = {"positions_get": 1}  # one IPC hiccup: the close fails (❗)
    post(p, XAUUSD_CLOSE, 506, T0 + timedelta(minutes=10))
    assert p.storage.get_follow_up("-1001:506:CLOSE:XAU_USD")["status"] == "FAILED"
    fake.hit_tp(a.ticket, 4498.30)  # A ends (the owner closed it from the alert, or TP)
    run(p.monitor_once())
    post(p, XAUUSD_MARKET, 510, T0 + timedelta(minutes=30))  # a new gold trade B
    (b,) = bot_positions(fake)
    edit(p, XAUUSD_CLOSE + " ✅", 506, T0 + timedelta(minutes=35), posted=T0 + timedelta(minutes=10))
    assert bot_positions(fake) == [b]  # B, which the channel never closed, is still open
    assert "msg 510" not in p.notifier.sent[-1]


def test_failed_close_retried_by_an_edit_still_closes_its_own_trade():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    fake.fail = {"positions_get": 1}
    post(p, XAUUSD_CLOSE, 506, T0 + timedelta(minutes=10))
    assert p.storage.get_follow_up("-1001:506:CLOSE:XAU_USD")["status"] == "FAILED"
    edit(p, XAUUSD_CLOSE + " ✅", 506, T0 + timedelta(minutes=12), posted=T0 + timedelta(minutes=10))
    assert bot_positions(fake) == []
    assert "msg 505" in p.notifier.sent[-1]


def test_failed_cancel_retried_by_an_edit_never_cancels_a_newer_pending_order():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.10, 4150.40)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 600, T0)
    fake.ipc_down = True
    post(p, "Cancel Pending Order on XAUUSD", 601, T0 + timedelta(minutes=5))
    fake.ipc_down = False
    assert p.storage.get_follow_up("-1001:601:CANCEL:XAU_USD")["status"] == "FAILED"
    (o,) = fake.orders.values()
    t = fake.fill_pending(o.ticket)  # the limit fills and hits its TP
    run(p.monitor_once())
    fake.hit_tp(t, 4142.80)
    run(p.monitor_once())
    post(p, XAUUSD_SELL_LIMIT.replace("4156.56", "4157.00"), 610, T0 + timedelta(minutes=40))
    assert len(fake.orders) == 1
    edit(p, "Cancel Pending Order on XAUUSD ❌", 601, T0 + timedelta(minutes=45), posted=T0 + timedelta(minutes=5))
    assert len(fake.orders) == 1  # the new limit stays


def test_rejected_be_retried_by_an_edit_never_moves_a_trade_opened_after_it():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, "Move SL to BE on XAUUSD", 700, T0)  # about a trade the bot does not have
    assert p.storage.get_follow_up("-1001:700:MOVE_SL_BE:XAU_USD")["status"] == "REJECTED"
    post(p, XAUUSD_MARKET, 705, T0 + timedelta(hours=2))
    (b,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4483.0, 4483.3)  # a BE would be possible
    edit(p, "Move SL to BE on XAUUSD ✅", 700, T0 + timedelta(hours=2, minutes=5), posted=T0)
    assert b.sl == 4470.38  # the fresh trade keeps its SL
    assert events(p, "follow_up")[-1] == ("REJECTED", "target_signal_not_found")


# ------------------------------------------------- partial-close-key-change

def test_edit_that_drops_the_symbol_does_not_close_another_half():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    start = pos.volume
    post(p, "Close 50% of XAUUSD now", 506, T0 + timedelta(minutes=10), reply_to=505)
    half = pos.volume
    assert half < start
    edit(p, "Close 50% now ✅", 506, T0 + timedelta(minutes=11), posted=T0 + timedelta(minutes=10), reply_to=505)
    assert pos.volume == half
    assert events(p, "follow_up")[-1][0] == "DUPLICATE"
    assert "вече е обработена" in p.notifier.sent[-1]


# ------------------------------------------------- raw-text-never-updated

def test_level_edit_after_a_management_edit_is_applied():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4486.0, 4486.3)  # above the entry: BE possible
    v1 = XAUUSD_MARKET + "\n\nTP1 hit ✅ move SL to BE"
    edit(p, v1, 505, T0 + timedelta(minutes=10))
    assert pos.sl == pos.price_open
    v2 = v1.replace("Target Profit 2 @ 4498.30", "Target Profit 2 @ 4492.00")  # the channel lowers TP2 later
    edit(p, v2, 505, T0 + timedelta(minutes=30))
    assert pos.tp == 4492.00
    assert pos.sl == pos.price_open  # BE kept
    assert events(p, "edit")[-1][0] == "AMENDED"
    assert not any("вече е обработена" in t for t in p.notifier.sent)


def test_levels_refused_next_to_a_new_command_stay_refused_on_a_later_edit():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    v1 = XAUUSD_MARKET.replace("Target Profit 2 @ 4498.30", "Target Profit 2 @ 4492.00") + "\n\nClose half now"
    edit(p, v1, 505, T0 + timedelta(minutes=10))
    assert pos.tp == 4498.30  # changed together with a command: not moved, the owner is told
    edit(p, v1 + " ", 505, T0 + timedelta(minutes=12))
    assert pos.tp == 4498.30  # a later edit does not move it silently either


# ------------------------------------------------- P4-edit-with-dropped-command-silent

@pytest.mark.parametrize("extra", ["\n\nYou can close now", "\n\nClosing this trade now at 4485.10",
                                   "\n\nMarket is reversing so close now"])
def test_edit_adding_a_command_the_bot_does_not_carry_out_is_reported(extra):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    n = len(p.notifier.sent)
    edit(p, XAUUSD_MARKET + extra, 505, T0 + timedelta(minutes=10))
    assert bot_positions(fake) == [pos]  # nothing is guessed
    assert any(t.startswith("⚠️") and "не го разбрах" in t for t in p.notifier.sent[n:])
    assert ("EDIT_UNCLEAR", None) in events(p, "edit")


def test_plain_level_edit_sends_no_unclear_notice():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    edit(p, XAUUSD_MARKET.replace("4498.30", "4495.00"), 505, T0 + timedelta(minutes=10))
    assert not any("не го разбрах" in t for t in p.notifier.sent)


def test_edit_adding_a_clear_close_still_closes():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    edit(p, XAUUSD_MARKET + "\n\nClose now", 505, T0 + timedelta(minutes=10))
    assert bot_positions(fake) == []
