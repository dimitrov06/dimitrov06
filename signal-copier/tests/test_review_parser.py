"""Final review of today's parser changes, from the channel's text to what MT5 receives.

- a real close or BE after "so" or after a line ending in a description word is never dropped without a notice
  (P1-not-commands-blank-real-commands-silently);
- a full close with a stray percent or "half" in another clause is a full close (P2-full-close-read-as-partial);
- a repeated TP label or labels against the prices are not guessed (P3-duplicate-tp-label-drops-tp2).
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest

from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import AUDUSD_SIGNAL, XAUUSD_MARKET, bot_positions, make, post
from signal_copier.models import FollowUpAction as A
from signal_copier.models import IncomingMessage, ParseStatus
from signal_copier.parser import SignalParser

P = SignalParser()


def parse(text, reply_to=505):
    return P.parse(IncomingMessage(chat_id=-1001, message_id=900, text=text, date=NOW, reply_to_msg_id=reply_to))


def actions(r):
    return sorted(f.action.value for f in r.follow_ups)


# ------------------------------------------------- P1-not-commands-blank-real-commands-silently

@pytest.mark.parametrize("text", [
    "Volatile NY session\nClose XAUUSD Manually now at 4485.10! (+0.50%)",
    "Momentum is gone, so\nClose XAUUSD Manually now at 4485.10!",
    "Bearish H4\nClose XAUUSD now",
])
def test_close_on_the_line_after_a_description_word_closes(text):
    assert actions(parse(text)) == ["CLOSE"]
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, NOW)
    post(p, text, 506, NOW + timedelta(minutes=10), reply_to=505)
    assert bot_positions(fake) == []


def test_be_and_close_half_on_two_lines_both_run():
    r = parse("TP1 Hit ✅ Move SL to BE\nClose half now")
    assert actions(r) == ["CLOSE_PARTIAL", "MOVE_SL_BE", "TP_HIT"] and not r.issues
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, NOW)
    (pos,) = bot_positions(fake)
    full = pos.volume
    fake.ticks["XAUUSD"] = (4484.60, 4484.90)  # TP1 reached: BE possible
    post(p, "TP1 Hit ✅ Move SL to BE\nClose half now", 506, NOW + timedelta(minutes=10), reply_to=505)
    assert pos.volume < full and pos.sl == pos.price_open


@pytest.mark.parametrize("text", ["Market is reversing so close XAUUSD now", "News is coming, so close it now",
                                  "Too risky to hold, so close all now", "TP2 so close now",
                                  "Move SL to BE close half now"])
def test_command_right_after_so_or_be_is_not_guessed_but_reported(text):
    r = parse(text)
    assert r.status is ParseStatus.UNCERTAIN and not r.follow_ups
    assert [i.reason for i in r.issues] == ["unclear_command"]
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, NOW)
    (pos,) = bot_positions(fake)
    post(p, text, 506, NOW + timedelta(minutes=10), reply_to=505)
    assert bot_positions(fake) == [pos]
    assert any(t.startswith("⚠️ Не разбрах") for t in p.notifier.sent)


@pytest.mark.parametrize("text, expected", [
    ("Price moved away, so cancel the pending order on XAUUSD", ["CANCEL"]),
    ("TP1 hit ✅ so move SL to BE", ["MOVE_SL_BE", "TP_HIT"]),
    ("Market is reversing so please close XAUUSD now", ["CLOSE"]),
])
def test_other_commands_after_so_are_still_commands(text, expected):
    r = parse(text)
    assert r.status is ParseStatus.FOLLOW_UP and actions(r) == expected


@pytest.mark.parametrize("text", ["So close!", "TP1 so close 🔥", "We are so close now to TP1", "Gold is so close now",
                                  "Too close to call", "Price will be close to entry", "It might be close"])
def test_description_with_so_or_be_close_stays_chatter(text):
    r = parse(text)
    assert r.status is ParseStatus.IGNORED and not r.follow_ups


# ------------------------------------------------- P2-full-close-read-as-partial

@pytest.mark.parametrize("text", [
    "Close XAUUSD now, 2% in profit", "Close XAUUSD now with 2% in profit", "Close now, 3% in the green",
    "Close XAUUSD now, 50% of the target reached", "Close XAUUSD now, we already took partials",
    "Close everything now, half of the move is done", "We already secured half\nClose XAUUSD now",
    "Close XAUUSD now, we made 2% today",
])
def test_full_close_with_a_stray_percent_or_half_is_a_full_close(text):
    assert actions(parse(text)) == ["CLOSE"]


def test_full_close_with_a_stray_percent_closes_the_whole_position():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, NOW)
    post(p, "Close AUDUSD now, 50% of the target reached", 502, NOW + timedelta(minutes=10), reply_to=501)
    assert bot_positions(fake) == []


@pytest.mark.parametrize("text, fraction", [
    ("Close AUDUSD 50% now", D("0.5")), ("close 30%", D("0.3")), ("Close half now", D("0.5")),
    ("Close 50% of XAUUSD now", D("0.5")), ("Close AUDUSD partially at 0.69500", D("0.5")),
    ("Partially close AUDUSD now", D("0.5")),
])
def test_partial_close_forms_are_still_partial(text, fraction):
    r = parse(text)
    assert [(f.action, f.fraction) for f in r.follow_ups] == [(A.CLOSE_PARTIAL, fraction)]


# ------------------------------------------------- P3-duplicate-tp-label-drops-tp2

def test_copy_pasted_tp_label_is_not_guessed():
    text = XAUUSD_MARKET.replace("Target Profit 2 @ 4498.30", "Target Profit 1 @ 4498.30")
    r = parse(text, reply_to=None)
    assert r.status is ParseStatus.UNCERTAIN and [i.reason for i in r.issues] == ["repeated_tp_label"]
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, text, 505, NOW)
    assert fake.market_sends() == []
    assert any(t.startswith("⚠️ Не разбрах") for t in p.notifier.sent)


def test_tp_labels_against_the_prices_are_not_guessed():
    text = XAUUSD_MARKET.replace("Target Profit 1 @ 4484.30", "Target Profit 2 @ 4484.30").replace(
        "Target Profit 2 @ 4498.30", "Target Profit 1 @ 4498.30")
    r = parse(text, reply_to=None)
    assert r.status is ParseStatus.UNCERTAIN and [i.reason for i in r.issues] == ["tp_labels_out_of_order"]


def test_tp_hit_note_under_the_tp_lines_is_still_not_a_level():
    r = parse(XAUUSD_MARKET + "\nTP1 hit ✅ 4485.10", reply_to=None)
    assert r.signals and r.signals[0].tp == [D("4484.30"), D("4498.30")]


def test_sell_signal_with_tps_in_label_order_still_parses():
    text = ("RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nSell Limit Order @ 4156.56\n"
            "Target Profit 1 @ 4153.58\nTarget Profit 2 @ 4142.80\nStop Loss @ 4162.55")
    r = parse(text, reply_to=None)
    assert r.status is ParseStatus.SIGNAL and r.signals[0].tp == [D("4153.58"), D("4142.80")]
