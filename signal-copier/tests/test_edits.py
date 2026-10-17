"""Edits of channel posts on a real-money account: an edit never revives a stale signal, never loosens a live
stop, and commands written into a post (CANCELLED, Close now, move SL to BE) are carried out or reported.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from conftest import NOW
from fake_mt5 import FakeMt5, StrictMt5
from owner_setup import (AUDUSD_SIGNAL, CHAT, XAUUSD_CANCEL, XAUUSD_CLOSE, XAUUSD_MARKET, XAUUSD_SELL_LIMIT,
                         bot_positions, edit, events, make, post, run, sig)
from signal_copier.listener.telegram import to_incoming
from signal_copier.executor.executor import ExecResult
from signal_copier.models import IncomingMessage
from signal_copier.risk import RiskDecision

T0 = NOW


# ------------------------------------------------- an edit never revives a rejected signal outside its window

def test_rejected_by_stop_then_resume_edit_hours_later_is_not_traded():
    fake = StrictMt5(demo=False)
    p = make(fake)
    run(p.handle_command("/stop"))
    post(p, AUDUSD_SIGNAL, 501, T0)
    assert sig(p, 501)["reason"] == "halted:manual_stop"
    run(p.handle_command("/resume"))
    edit(p, AUDUSD_SIGNAL.replace("Target Profit 1 @ 0.69446", "Target Profit 1 @ 0.69446 ✅"), 501,
         T0 + timedelta(hours=2))
    assert fake.market_sends() == [] and sig(p, 501)["status"] == "REJECTED"
    assert events(p, "edit")[-1][0] == "EDIT_IGNORED"
    assert "Не търгувам" in p.notifier.sent[-1] and "halted:manual_stop" in p.notifier.sent[-1]


def test_rejected_for_max_open_then_sl_hit_appended_is_not_traded():
    fake = StrictMt5(demo=False)
    p = make(fake, max_open_positions=0)
    post(p, XAUUSD_MARKET, 505, T0)
    assert sig(p, 505)["reason"] == "max_open_positions"
    p.settings.risk.max_open_positions = 3  # the slots are free again
    edit(p, XAUUSD_MARKET + "\nSL HIT ❌", 505, T0 + timedelta(hours=5))
    assert fake.market_sends() == [] and sig(p, 505)["status"] == "REJECTED"


def test_daily_stop_rejection_is_not_traded_next_day_after_edit():
    fake = StrictMt5(demo=False)
    p = make(fake)
    p.storage.set_state(f"day_start_nav:{T0.date()}", "10700")  # -6.5% today
    post(p, XAUUSD_MARKET, 505, T0)
    assert sig(p, 505)["reason"] == "daily_loss_limit"
    edit(p, XAUUSD_MARKET + " ✅", 505, T0 + timedelta(days=1))
    assert fake.market_sends() == [] and events(p, "edit")[-1][0] == "EDIT_IGNORED"


def test_rejected_sell_limit_is_not_placed_by_a_result_edit_hours_later():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake, max_open_positions=0)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    p.settings.risk.max_open_positions = 3
    edit(p, XAUUSD_SELL_LIMIT + "\n✅ TP1 hit +100 pips", 800, T0 + timedelta(hours=5))
    assert fake.orders == {} and fake.sent == []


@pytest.mark.parametrize("appended", ["\nRunning", "\n✅ Closed in profit (+1.43%)"])
def test_signal_the_channel_already_closed_is_not_traded_by_a_later_edit(appended):
    """Rejected under /stop, closed by the channel 1 h later (the close finds nothing), edited 5 h later."""
    fake = StrictMt5(demo=False)
    p = make(fake)
    run(p.handle_command("/stop"))
    post(p, XAUUSD_MARKET, 505, T0)
    run(p.handle_command("/resume"))
    post(p, XAUUSD_CLOSE, 506, T0 + timedelta(hours=1), reply_to=505)
    edit(p, XAUUSD_MARKET + appended, 505, T0 + timedelta(hours=5))
    assert fake.market_sends() == [] and sig(p, 505)["status"] == "REJECTED"


def test_signal_delivered_too_late_is_not_traded_by_a_later_edit():
    fake = StrictMt5(demo=False)
    p = make(fake)
    p.test_clock["t"] = T0 + timedelta(minutes=3)  # delivered 3 min after the post
    run(p.handle_message(to_incoming(CHAT, NS(id=505, raw_text=XAUUSD_MARKET, date=T0, edit_date=None,
                                              reply_to=None), edited=False)))
    assert sig(p, 505)["reason"].startswith("signal_too_old")
    edit(p, XAUUSD_MARKET + " ✅", 505, T0 + timedelta(minutes=10))
    assert fake.market_sends() == [] and events(p, "edit")[-1][0] == "EDIT_IGNORED"


def test_signal_rejected_for_a_price_move_is_not_traded_when_price_returns_and_the_post_is_edited():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4483.50, 4483.80)  # 4.1 towards TP: more than 30% of the 9.31 SL distance
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    assert sig(p, 505)["reason"].startswith("price_moved_too_far")
    fake.ticks["XAUUSD"] = (4479.50, 4479.80)
    edit(p, XAUUSD_MARKET + " ✅", 505, T0 + timedelta(hours=3))
    assert fake.market_sends() == []


def test_cancelled_sell_limit_is_not_placed_by_a_later_edit():
    """The real 'Cancel Pending Order' reply finds no trade (the limit was rejected); the edit adds 'Cancelled'."""
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake, max_open_positions=0)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    p.settings.risk.max_open_positions = 3
    post(p, XAUUSD_CANCEL, 801, T0 + timedelta(hours=1), reply_to=800)
    edit(p, XAUUSD_SELL_LIMIT + "\nCancelled ❌", 800, T0 + timedelta(hours=5))
    assert fake.orders == {} and fake.sent == []


def test_edit_that_adds_the_sl_right_away_is_traded():
    """Control: the reason an edit exists. SL added 20 s after the post: evaluated and traded."""
    fake = StrictMt5(demo=False)
    p = make(fake)
    no_sl = XAUUSD_MARKET.replace("\nStop Loss @ 4470.38", "")
    post(p, no_sl, 505, T0)
    assert sig(p, 505)["reason"] == "missing_sl"
    edit(p, XAUUSD_MARKET, 505, T0 + timedelta(seconds=20))
    assert len(fake.market_sends()) == 1 and sig(p, 505)["status"] == "EXECUTED"


def test_edit_adding_the_sl_counts_age_from_the_post_not_the_edit():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET.replace("\nStop Loss @ 4470.38", ""), 505, T0)
    edit(p, XAUUSD_MARKET, 505, T0 + timedelta(minutes=10))
    assert fake.market_sends() == []
    assert "публикуван е преди 602 s" in p.notifier.sent[-1]


def test_edit_of_a_post_first_seen_as_chatter_counts_age_from_the_post():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, "XAUUSD signal coming soon", 505, T0)
    edit(p, XAUUSD_MARKET, 505, T0 + timedelta(minutes=10))
    assert fake.market_sends() == [] and events(p, "edit")[-1][0] == "EDIT_IGNORED"


def test_signal_refused_by_the_broker_is_not_resent_on_edit():
    """10027 AutoTrading disabled -> FAILED. A quick legit edit is reported, never silently 'EXECUTED'."""
    fake = StrictMt5(demo=False)
    p = make(fake)
    fake.algo_trading = False
    post(p, XAUUSD_MARKET, 505, T0)
    assert sig(p, 505)["status"] == "FAILED"
    fake.algo_trading = True
    sends = len(fake.sent)
    edit(p, XAUUSD_MARKET.replace("4498.30", "4499.30"), 505, T0 + timedelta(seconds=30))
    assert len(fake.sent) == sends and sig(p, 505)["status"] == "FAILED"
    assert "грешка/отказ от брокера" in p.notifier.sent[-1]


def test_limit_refused_as_invalid_price_is_not_resent_by_a_quick_edit():
    """10015: gold spiked above the Sell Limit as it arrived. The channel moves the levels 3.00 up 40 s later:
    the owner is told, nothing is sent again automatically and the signal does not show EXECUTED."""
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4157.00, 4157.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 802, T0)
    assert sig(p, 802)["status"] == "FAILED" and fake.orders == {}
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    sends = len(fake.sent)
    moved = (XAUUSD_SELL_LIMIT.replace("4156.56", "4159.56").replace("4162.55", "4165.55")
             .replace("4153.58", "4156.58").replace("4142.80", "4145.80"))
    edit(p, moved, 802, T0 + timedelta(seconds=40))
    assert len(fake.sent) == sends and sig(p, 802)["status"] == "FAILED"
    assert "Не търгувам" in p.notifier.sent[-1]


def test_open_signal_reports_a_rejected_order_row_instead_of_skipping_it():
    fake = StrictMt5(demo=False)
    p = make(fake)
    fake.algo_trading = False
    post(p, XAUUSD_MARKET, 505, T0)
    fake.algo_trading = True
    (s,) = p.parser.parse(IncomingMessage(chat_id=-1001, message_id=505, text=XAUUSD_MARKET, date=T0)).signals
    info = run(p.client.instrument("XAU_USD"))
    r: ExecResult = run(p.executor.open_signal(s, RiskDecision(approved=True, units=Decimal("0.31")), info))
    assert r.skipped == [] and r.opened == [] and "REJECTED по-рано" in r.errors[0]
    assert len(fake.market_sends()) == 1  # only the first, refused send


# ------------------------------------------------------------- an edit never loosens a live stop

def _xau_at_be(fake, p):
    post(p, XAUUSD_MARKET, 505, T0)  # buy at ask 4479.80, SL 4470.38, TP2 4498.30
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4485.10, 4485.40)  # TP1 4484.30 reached
    post(p, "TP1 hit ✅ move SL to BE", 506, T0 + timedelta(minutes=12), reply_to=505)
    assert pos.sl == 4479.80
    return pos


@pytest.mark.parametrize("old,new", [("Target Profit 2 @ 4498.30", "Target Profit 2 @ 4505.00"),
                                     ("Target Profit 1 @ 4484.30", "Target Profit 1 @ 4484.30 ✅ hit at 4485.10"),
                                     ("Target Profit 1 @ 4484.30", "Target Profit 1 @ 4485.00")])
def test_tp_edit_keeps_the_stop_at_break_even(old, new):
    fake = StrictMt5(demo=False)
    p = make(fake)
    pos = _xau_at_be(fake, p)
    sltp = len(fake.sltp_sends())
    edit(p, XAUUSD_MARKET.replace(old, new), 505, T0 + timedelta(minutes=14))
    assert pos.sl == 4479.80
    assert pos.tp == (4505.00 if "4505" in new else 4498.30)
    if "4505" not in new:  # the live TP (TP2) did not change: nothing is sent (no 10025 "No changes" error)
        assert len(fake.sltp_sends()) == sltp
    assert not any(t.startswith("❗") for t in p.notifier.sent)


@pytest.mark.parametrize("wider", ["4460.38", "4407.38"])  # a correction, a digit-swap typo (~23% risk)
def test_edit_that_widens_the_sl_is_refused_and_reported(wider):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    edit(p, XAUUSD_MARKET.replace("4470.38", wider), 505, T0 + timedelta(minutes=3))
    assert pos.sl == 4470.38  # 0.31 lots sized for 3% would risk ~6.2% at 4460.38
    assert "не го разхлабвам" in p.notifier.sent[-1]


def test_edit_that_tightens_the_sl_is_applied():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    edit(p, XAUUSD_MARKET.replace("4470.38", "4472.38"), 505, T0 + timedelta(minutes=3))
    assert pos.sl == 4472.38


def test_move_sl_further_away_is_refused():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    post(p, "Move SL to 4460", 506, T0 + timedelta(minutes=3), reply_to=505)
    assert pos.sl == 4470.38 and fake.sltp_sends() == []
    assert "не разхлабвам" in p.notifier.sent[-1]


# ------------------------------------------------- result notes are not TP levels

@pytest.mark.parametrize("note", ["\nTarget Profit 1 Hit ✅ @ 4484.50", "\nTP1 hit ✅ 4484.50"])
def test_hit_note_appended_to_the_post_does_not_move_the_live_tp(note):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4482.00, 4482.30)
    edit(p, XAUUSD_MARKET + note, 505, T0 + timedelta(minutes=12))
    assert (pos.tp, pos.sl) == (4498.30, 4470.38)
    assert sig(p, 505)["tps"] == '["4484.30", "4498.30"]'


@pytest.mark.parametrize("edited", [XAUUSD_MARKET.replace("4498.30", "4490.00") + "\nTP1 hit ✅",
                                    XAUUSD_MARKET + "\nTP3 hit ✅ 4510"])
def test_levels_changed_together_with_a_command_are_not_moved(edited):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    edit(p, edited, 505, T0 + timedelta(minutes=12))
    assert pos.tp == 4498.30
    assert any("Не местя SL/TP автоматично" in t for t in p.notifier.sent)


# ------------------------------------------------- commands written into the signal post

@pytest.mark.parametrize("suffix", ["\n\nCANCELLED ❌", "\n❌ Cancelled", "\nOrder cancelled", "\nCancel this order"])
def test_sell_limit_edited_to_cancelled_is_cancelled(suffix):
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    assert len(fake.orders) == 1
    edit(p, XAUUSD_SELL_LIMIT + suffix, 800, T0 + timedelta(minutes=20))
    assert fake.orders == {}
    assert "отменена чакаща поръчка" in p.notifier.sent[-1]
    edit(p, XAUUSD_SELL_LIMIT + suffix + " ", 800, T0 + timedelta(minutes=21))  # edited again: runs once
    assert events(p, "follow_up")[-1][0] == "DUPLICATE"


def test_market_post_edited_to_close_now_closes():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    edit(p, XAUUSD_MARKET + "\n\n‼️ Close now at 4476.10", 505, T0 + timedelta(minutes=30))
    assert bot_positions(fake) == []


def test_post_edited_with_tp1_hit_and_be_moves_the_stop():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4485.10, 4485.40)
    edit(p, XAUUSD_MARKET + "\nTP1 hit ✅ 4485.10 move SL to BE", 505, T0 + timedelta(minutes=12))
    assert (pos.sl, pos.tp) == (4479.80, 4498.30)


def test_management_text_already_in_the_template_is_not_run_on_edit():
    fake = FakeMt5(demo=False)
    p = make(fake)
    text = "XAUUSD BUY NOW\nSL 4470.38\nTP1 4484.30\nTP2 4498.30\nClose half at TP1 and move SL to BE"
    post(p, text, 505, T0)
    (pos,) = bot_positions(fake)
    vol = pos.volume
    edit(p, text.replace("4498.30", "4499.30"), 505, T0 + timedelta(minutes=5))
    assert pos.volume == vol and pos.sl == 4470.38 and pos.tp == 4499.30


# ------------------------------------------------- an edited SL command is a correction

def test_edited_move_sl_with_a_new_price_is_applied():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4490.00, 4490.30)
    post(p, "Move SL to 4480", 507, T0 + timedelta(minutes=20), reply_to=505)
    assert pos.sl == 4480.0
    edit(p, "Move SL to 4488", 507, T0 + timedelta(minutes=21), posted=T0 + timedelta(minutes=20), reply_to=505)
    assert pos.sl == 4488.0


def test_move_sl_refused_by_price_then_corrected_by_edit_is_applied():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4490.00, 4490.30)
    post(p, "Move SL to 4498", 507, T0 + timedelta(minutes=20), reply_to=505)  # above Bid: not possible yet
    assert pos.sl == 4470.38 and p.storage.follow_ups_with_status("WAITING")
    edit(p, "Move SL to 4488", 507, T0 + timedelta(minutes=21), posted=T0 + timedelta(minutes=20), reply_to=505)
    assert pos.sl == 4488.0


def test_audusd_move_sl_digit_swap_is_corrected_by_edit():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["AUDUSD"] = (0.69460, 0.69462)
    post(p, "Move SL to 0.69440", 503, T0 + timedelta(minutes=20), reply_to=501)
    assert pos.sl == 0.6944
    edit(p, "Move SL to 0.69404", 503, T0 + timedelta(minutes=21), posted=T0 + timedelta(minutes=20), reply_to=501)
    # 0.69404 is further from the price than 0.69440: the live (tighter) stop stays, and the owner is told
    assert pos.sl == 0.6944 and "не разхлабвам" in p.notifier.sent[-1]


def test_edited_close_is_not_repeated_and_the_owner_is_told():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    post(p, "Close half", 506, T0 + timedelta(minutes=5), reply_to=505)
    (pos,) = bot_positions(fake)
    vol = pos.volume
    edit(p, "Close half now", 506, T0 + timedelta(minutes=6), posted=T0 + timedelta(minutes=5), reply_to=505)
    assert pos.volume == vol and "не я повтарям" in p.notifier.sent[-1]
