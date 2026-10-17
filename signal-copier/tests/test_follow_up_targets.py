"""Channel commands on a real-money account: which trade a command acts on, SL commands that are not prices, and
a break-even that MT5 cannot accept yet.
"""

from datetime import timedelta

import pytest

from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import (AUDUSD_CLOSE, AUDUSD_SIGNAL, XAUUSD_CANCEL, XAUUSD_CLOSE, XAUUSD_MARKET, XAUUSD_SELL_LIMIT,
                         bot_positions, events, make, post, run, sig)
from signal_copier.models import IncomingMessage, ParseStatus
from signal_copier.parser import SignalParser

T0 = NOW


# ------------------------------------------------- SL commands whose number is not a price

@pytest.mark.parametrize("text", [
    "Move SL to 20 pips in profit", "Set SL at 30 pips profit", "Move SL to 1:1", "Move SL 15 pips above entry",
    "Set SL at 50% of the move", "Move the stop to 30 points profit", "New SL: 10 pips below current price",
    "Move stop loss to 20pips", "Move SL to 1.5R", "Shift SL to 10 pips profit", "XAUUSD move SL to 20 pips profit",
    "SL to 1:1", "Move SL to +20 pips",
])
def test_sl_command_with_a_distance_is_uncertain(text):
    r = SignalParser().parse(IncomingMessage(chat_id=-1001, message_id=9, text=text, date=T0, reply_to_msg_id=5))
    assert r.status is ParseStatus.UNCERTAIN and not r.follow_ups


@pytest.mark.parametrize("text,price", [("Move SL to 4488 (+80 pips)", "4488"), ("Move SL to 4480.", "4480"),
                                        ("New SL 4475.5", "4475.5")])
def test_sl_command_with_a_price_still_parses(text, price):
    r = SignalParser().parse(IncomingMessage(chat_id=-1001, message_id=9, text=text, date=T0, reply_to_msg_id=5))
    assert r.status is ParseStatus.FOLLOW_UP and str(r.follow_ups[0].price) == price


def test_move_sl_in_pips_leaves_the_real_stop_and_notifies():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    post(p, "Move SL to 20 pips in profit", 506, T0 + timedelta(minutes=10), reply_to=505)
    assert pos.sl == 4470.38 and fake.sltp_sends() == []
    assert "НЕ търгувам" in p.notifier.sent[-1]


@pytest.mark.parametrize("text", ["Move SL to 20", "Move SL to 1"])
def test_move_sl_to_a_bogus_price_is_never_sent(text):
    """A number that parses as a price but would remove the stop (BUY SL 20 under 4480) is refused."""
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    post(p, text, 506, T0 + timedelta(minutes=10), reply_to=505)
    assert pos.sl == 4470.38 and fake.sltp_sends() == []


def test_move_sl_far_beyond_the_entry_is_refused():
    fake = StrictMt5(demo=False)
    fake.ticks["AUDUSD"] = (0.69400, 0.69402)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    (pos,) = bot_positions(fake)
    post(p, "Move SL to 4475", 503, T0 + timedelta(minutes=10), reply_to=501)  # a gold price on the AUDUSD trade
    assert pos.sl == 0.69311 and "твърде далеч" in p.notifier.sent[-1]
    assert p.storage.follow_ups_with_status("WAITING") == []


# ------------------------------------------------- BE while the price is behind the entry

def test_be_behind_entry_waits_and_is_applied_when_price_allows():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)  # filled at 0.69402
    (pos,) = bot_positions(fake)
    fake.ticks["AUDUSD"] = (0.69395, 0.69397)  # pulled back below the entry
    post(p, "TP1 hit ✅ move SL to BE", 503, T0 + timedelta(minutes=10), reply_to=501)
    assert pos.sl == 0.69311 and fake.sltp_sends() == []  # MT5 would refuse it (10016): not sent
    assert "⏳" in p.notifier.sent[-2] or "⏳" in p.notifier.sent[-1]
    run(p.monitor_once())
    assert pos.sl == 0.69311  # still behind
    fake.ticks["AUDUSD"] = (0.69420, 0.69422)
    run(p.monitor_once())
    assert pos.sl == 0.69402
    assert any("(изчакано)" in t for t in p.notifier.sent)
    run(p.monitor_once())  # done once
    assert len(fake.sltp_sends()) == 1


def test_waiting_be_ends_when_the_trade_closes():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["AUDUSD"] = (0.69395, 0.69397)
    post(p, "Move SL to BE", 503, T0 + timedelta(minutes=10), reply_to=501)
    fake.positions.pop(pos.ticket)  # stopped out at the broker
    run(p.monitor_once())
    assert p.storage.follow_ups_with_status("WAITING") == []


def test_second_be_message_is_not_an_error():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["AUDUSD"] = (0.69450, 0.69452)
    post(p, "TP1 hit ✅ move SL to BE", 503, T0 + timedelta(minutes=10), reply_to=501)
    post(p, "Trade is risk free, SL at BE", 504, T0 + timedelta(minutes=12), reply_to=501)
    assert pos.sl == 0.69402 and len(fake.sltp_sends()) == 1  # no 10025 "No changes"
    assert not any(t.startswith("❗") for t in p.notifier.sent)


# ------------------------------------------------- which trade a command acts on

def test_close_replying_to_a_tp_hit_post_finds_the_signal():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    post(p, "TP1 hit ✅", 502, T0 + timedelta(minutes=10), reply_to=501)
    post(p, AUDUSD_CLOSE, 503, T0 + timedelta(minutes=30), reply_to=502)
    assert bot_positions(fake) == []
    post(p, AUDUSD_SIGNAL.replace("0.69401", "0.69402"), 510, T0 + timedelta(hours=1))
    post(p, "Close now", 511, T0 + timedelta(hours=1, minutes=5), reply_to=502)  # symbol-less, same chain
    assert len(bot_positions(fake)) == 1  # the chain leads to the closed 501, never to 510


def test_close_by_symbol_ignores_closed_signals():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (a,) = bot_positions(fake)
    fake.hit_tp(a.ticket, 4498.30)
    run(p.monitor_once())
    post(p, XAUUSD_MARKET.replace("4479.69", "4479.70"), 520, T0 + timedelta(hours=3))
    post(p, "Close XAUUSD now", 521, T0 + timedelta(hours=3, minutes=10))
    assert bot_positions(fake) == []


def test_close_by_symbol_finds_an_open_trade_older_than_the_lookback():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    post(p, "Close XAUUSD now", 521, T0 + timedelta(hours=26))
    assert bot_positions(fake) == []


def test_close_by_symbol_with_two_open_trades_is_ambiguous():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    post(p, XAUUSD_MARKET.replace("4479.69", "4479.70"), 520, T0 + timedelta(hours=3))
    post(p, "Close XAUUSD now", 521, T0 + timedelta(hours=3, minutes=10))
    assert len(bot_positions(fake)) == 2 and events(p, "follow_up")[-1] == ("REJECTED", "ambiguous_target")


@pytest.mark.parametrize("text", [XAUUSD_CLOSE, "Close half XAUUSD", "XAUUSD move SL to BE", XAUUSD_CANCEL])
def test_command_naming_another_symbol_than_the_replied_signal_does_nothing(text):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    post(p, XAUUSD_MARKET, 505, T0 + timedelta(minutes=1))
    fake.ticks["AUDUSD"] = (0.69450, 0.69452)
    before = {t: (x.volume, x.sl) for t, x in fake.positions.items()}
    post(p, text, 506, T0 + timedelta(minutes=20), reply_to=501)
    assert {t: (x.volume, x.sl) for t, x in fake.positions.items()} == before
    assert events(p, "follow_up")[-1][1].startswith("symbol_mismatch")
    assert "XAU_USD≠AUD_USD" in p.notifier.sent[-1]


def test_two_closes_in_one_message_close_both():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    post(p, XAUUSD_MARKET, 505, T0 + timedelta(minutes=1))
    post(p, AUDUSD_CLOSE + "\n" + XAUUSD_CLOSE, 540, T0 + timedelta(minutes=30))
    assert bot_positions(fake) == []


def test_one_command_for_two_symbols_is_not_guessed():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    post(p, XAUUSD_MARKET, 505, T0 + timedelta(minutes=1))
    post(p, "Close AUDUSD and XAUUSD now", 540, T0 + timedelta(minutes=30))
    assert len(bot_positions(fake)) == 2 and "command_for_several_symbols" in p.notifier.sent[-1]


def test_follow_up_written_by_an_older_version_still_counts_as_done():
    """Rows stored before the symbol was part of the key: an edit after the upgrade never closes half twice."""
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    post(p, "XAUUSD close half", 506, T0 + timedelta(minutes=5), reply_to=505)
    p.storage.conn.execute("UPDATE follow_ups SET key='-1001:506:CLOSE_PARTIAL'")
    (pos,) = bot_positions(fake)
    vol = pos.volume
    from owner_setup import edit
    edit(p, "XAUUSD close half now", 506, T0 + timedelta(minutes=6), posted=T0 + timedelta(minutes=5), reply_to=505)
    assert pos.volume == vol


# ------------------------------------------------- a close and a new signal in one message

def test_close_and_new_signal_in_one_unreplied_message():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (old,) = bot_positions(fake)
    new = ("‼️ Close XAUUSD Manually now at 4479.60! (+0.1%)\nRTED Investing - Premium Trading Signals 🏆\n"
           "XAUUSD\nSell now @ 4479.50\nTarget Profit 1 @ 4475.00\nTarget Profit 2 @ 4465.00\nStop Loss @ 4488.00")
    post(p, new, 520, T0 + timedelta(minutes=30))
    assert old.ticket not in fake.positions
    (pos,) = bot_positions(fake)
    assert pos.type == fake.POSITION_TYPE_SELL and sig(p, 520)["status"] == "EXECUTED"


def test_cancel_and_new_limit_in_one_message_cancels_the_old_one():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    (old,) = fake.orders
    post(p, XAUUSD_CANCEL + "\n" + XAUUSD_SELL_LIMIT.replace("4156.56", "4158.00"), 801, T0 + timedelta(minutes=30))
    assert old not in fake.orders and len(fake.orders) == 1


def test_two_break_even_commands_in_one_message_move_both():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    post(p, XAUUSD_MARKET, 505, T0 + timedelta(minutes=1))
    fake.ticks["AUDUSD"] = (0.69450, 0.69452)
    fake.ticks["XAUUSD"] = (4485.10, 4485.40)
    post(p, "XAUUSD move SL to BE\nAUDUSD move SL to BE", 540, T0 + timedelta(minutes=30))
    assert all(x.sl == x.price_open for x in bot_positions(fake))


def test_reply_to_a_message_the_bot_never_saw_does_not_fall_back_to_the_symbol():
    """Message 400 was posted before the bot's history: it may be another XAUUSD signal the bot never traded."""
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    post(p, "Close XAUUSD now", 521, T0 + timedelta(minutes=10), reply_to=400)
    assert len(bot_positions(fake)) == 1 and events(p, "follow_up")[-1] == ("REJECTED", "target_signal_not_found")
    post(p, "Good morning traders", 510, T0 + timedelta(minutes=11))  # control: a stored post without a signal
    post(p, "Close XAUUSD now", 522, T0 + timedelta(minutes=12), reply_to=510)
    assert bot_positions(fake) == []


def test_retried_close_never_closes_the_new_signal_of_its_own_message():
    """'Close XAUUSD' + a new XAUUSD SELL in one post. The close fails (MT5 does not answer), the old buy then
    stops out, and the channel edits the post: the retried close must not pick the SELL from the same post."""
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (old,) = bot_positions(fake)
    new = ("‼️ Close XAUUSD Manually now at 4479.60! (+0.1%)\nRTED Investing - Premium Trading Signals 🏆\n"
           "XAUUSD\nSell now @ 4479.50\nTarget Profit 1 @ 4475.00\nTarget Profit 2 @ 4465.00\nStop Loss @ 4488.00")
    fake.fail = {"positions_get": 1}
    post(p, new, 520, T0 + timedelta(minutes=30))
    assert old.ticket in fake.positions and sig(p, 520)["status"] == "EXECUTED"
    fake.hit_tp(old.ticket, 4470.38)
    run(p.monitor_once())
    from owner_setup import edit
    edit(p, new + " ✅", 520, T0 + timedelta(minutes=32), posted=T0 + timedelta(minutes=30))
    (pos,) = bot_positions(fake)
    assert pos.type == fake.POSITION_TYPE_SELL
