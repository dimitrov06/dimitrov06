"""What the channel writes vs what the bot does, on the owner's real-money setup.

- chatter that only contains a command word ("very close to TP1", "don't close", "weekly close", "break even
  higher") never closes, cancels or moves anything (C14);
- a partial close is a partial close, also with the symbol or a price in between (C14);
- an update about a running trade ("XAUUSD Buy - New Stop Loss @ x", "TP1 @ x reached, move SL to BE") is never
  a new trade, and a post that quotes a whole order next to "Cancel" / "Closed manually" is not traded (C15);
- an order type the parser does not recognise is never taken at market (C15).
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest

from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import (AUDUSD_CLOSE, AUDUSD_SIGNAL, XAUUSD_CANCEL, XAUUSD_CLOSE, XAUUSD_MARKET, XAUUSD_SELL_LIMIT,
                         bot_positions, edit, make, post, sig)
from signal_copier.models import EntryType, FollowUpAction, IncomingMessage, ParseStatus
from signal_copier.parser import SignalParser

T0 = NOW
PARSER = SignalParser()


def parse(text, reply_to=505, edited=False):
    return PARSER.parse(IncomingMessage(chat_id=-1001, message_id=900, text=text, date=T0, reply_to_msg_id=reply_to,
                                        edited=edited))


def actions(r):
    return [f.action for f in r.follow_ups]


def gold(line, tp1, tp2, sl):
    return (f"RTED Investing - Premium Trading Signals 🏆\nXAUUSD\n{line}\nTarget Profit 1 @ {tp1}\n"
            f"Target Profit 2 @ {tp2}\nStop Loss @ {sl}")


def warned(p):
    return any(t.startswith("⚠️ Не разбрах") for t in p.notifier.sent)


# ================================================= C14: chatter is not a command

CLOSE_CHATTER = [
    "Very close to TP1 🔥 hold!", "Don't close yet, hold for TP2", "Do not close your trades",
    "Don’t close yet", "Price is close to our entry, be ready", "XAUUSD getting close to TP1", "Hold, don't exit",
    "XAUUSD is close to Target Profit 1 🔥", "Gold very close to TP2 now 🚀", "XAUUSD Buy running nicely, close to TP1",
    "Exit strategy for GOLD: we hold until TP2", "Market close soon, be careful",
    "Weekly close above 4400 is bullish for gold", "We are close now to TP1",
]


@pytest.mark.parametrize("text", CLOSE_CHATTER)
def test_chatter_with_the_word_close_is_not_a_close(text):
    """close-regex-false-positives / close-word-false-positive: descriptive or negated, so nothing is done."""
    for reply_to in (505, None):
        r = parse(text, reply_to=reply_to)
        assert r.status in (ParseStatus.IGNORED, ParseStatus.UNCERTAIN) and not r.follow_ups, r


@pytest.mark.parametrize("text, reason", [
    ("Partial exit at TP1", "conditional_command"),
    ("Close half at TP1 and move SL to BE", "conditional_command"),
    ("We will move SL to break even once TP1 is hit on XAUUSD", "conditional_command"),
    ("If price reaches 4150 first, cancel the order", "conditional_command"),
    ("Gold could break even higher", "unclear_command"),
    ("Closing XAUUSD now", "unclear_command"),
    ("Close 3 lots", "unclear_command"),
    ("Close at 23:00", "unclear_close_command"),
    ("Close now (43%)", "unclear_close_command"),  # a size or a P/L?
    ("Don't close yet, hold for TP2", "negated_command"),
    ("Don't close, move SL to BE", "negated_command"),
    ("Don't wait close now", "negated_command"),  # a real close may hide behind a negation: told, never dropped
    ("We will close now", "conditional_command"),
    ("You should move SL to BE", "conditional_command"),
])
def test_a_plan_or_an_unclear_command_is_not_carried_out_and_is_reported(text, reason):
    r = parse(text)
    assert r.status is ParseStatus.UNCERTAIN and not r.follow_ups
    assert [i.reason for i in r.issues] == [reason]


@pytest.mark.parametrize("text", ["Don't cancel your XAUUSD order yet", "Don’t cancel your XAUUSD order yet",
                                  "Do not cancel the limit"])
def test_negated_cancel_is_not_a_cancel(text):
    assert FollowUpAction.CANCEL not in actions(parse(text, reply_to=None))


@pytest.mark.parametrize("text, expected", [
    (AUDUSD_CLOSE, [FollowUpAction.CLOSE]), (XAUUSD_CLOSE, [FollowUpAction.CLOSE]),
    (XAUUSD_CANCEL, [FollowUpAction.CANCEL]), ("Close now ❌", [FollowUpAction.CLOSE]),
    ("close", [FollowUpAction.CLOSE]), ("close all", [FollowUpAction.CLOSE]), ("Exit now", [FollowUpAction.CLOSE]),
    ("Close XAUUSD now", [FollowUpAction.CLOSE]), ("AUDUSD close now", [FollowUpAction.CLOSE]),
    ("Close the trade", [FollowUpAction.CLOSE]), ("Close US30 now", [FollowUpAction.CLOSE]),
    ("‼️ Close now at 4476.10", [FollowUpAction.CLOSE]), ("Not yet at TP1. Close now!", [FollowUpAction.CLOSE]),
    ("TP1 hit ✅ close now", [FollowUpAction.TP_HIT, FollowUpAction.CLOSE]),
    ("Close now, the market may reverse", [FollowUpAction.CLOSE]),
    ("Cancel Pending Order on XAUUSD. It will not trigger before NY", [FollowUpAction.CANCEL]),
    ("Move to breakeven", [FollowUpAction.MOVE_SL_BE]),
    ("TP1 hit ✅ Breakeven", [FollowUpAction.MOVE_SL_BE, FollowUpAction.TP_HIT]),
    ("Order canceled", [FollowUpAction.CANCEL]),
])
def test_real_command_forms_are_still_commands(text, expected):
    r = parse(text)
    assert r.status is ParseStatus.FOLLOW_UP and actions(r) == expected
    if text in (AUDUSD_CLOSE, XAUUSD_CLOSE):
        assert r.follow_ups[0].price in (D("0.69530"), D("4485.10"))


@pytest.mark.parametrize("text", ["Very close to TP1 🔥 hold!", "Don't close yet, hold for TP2",
                                  "Do not close your trades", "Partial exit at TP1"])
def test_chatter_replying_to_the_audusd_trade_leaves_it_open(text):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    (pos,) = bot_positions(fake)
    vol = pos.volume
    post(p, text, 502, T0 + timedelta(minutes=10), reply_to=501)
    assert bot_positions(fake) == [pos] and pos.volume == vol
    assert not any(r["action"] == fake.TRADE_ACTION_DEAL and "position" in r for r in fake.sent)


def test_close_to_our_entry_reply_keeps_the_pending_limit():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    post(p, "Price is close to our entry, be ready", 801, T0 + timedelta(minutes=5), reply_to=800)
    assert len(fake.orders) == 1 and not any(r["action"] == fake.TRADE_ACTION_REMOVE for r in fake.sent)


@pytest.mark.parametrize("text", ["XAUUSD getting close to TP1", "Gold very close to TP2 now 🚀",
                                  "Exit strategy for GOLD: we hold until TP2",
                                  "Weekly close above 4400 is bullish for gold"])
def test_chatter_naming_gold_without_reply_keeps_the_gold_trade(text):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    post(p, text, 506, T0 + timedelta(minutes=10))
    assert bot_positions(fake) == [pos]


def test_dont_cancel_keeps_the_pending_order():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    post(p, "Don't cancel your XAUUSD order yet", 801, T0 + timedelta(minutes=5))
    assert len(fake.orders) == 1 and not any(r["action"] == fake.TRADE_ACTION_REMOVE for r in fake.sent)


@pytest.mark.parametrize("text", ["We will move SL to break even once TP1 is hit on XAUUSD",
                                  "Gold could break even higher"])
def test_conditional_or_descriptive_break_even_keeps_the_stop_and_notifies(text):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4482.10, 4482.40)  # in profit: a real server would accept a BE now
    post(p, text, 508, T0 + timedelta(minutes=10))
    assert pos.sl == 4470.38 and fake.sltp_sends() == []
    assert warned(p)


def test_chatter_added_to_the_signal_post_by_an_edit_does_nothing():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    edit(p, XAUUSD_SELL_LIMIT + "\nDon't cancel, very close to our entry 🔥", 800, T0 + timedelta(minutes=20))
    assert len(fake.orders) == 1


# ================================================= C14: a partial close is partial

@pytest.mark.parametrize("text, fraction", [
    ("Close AUDUSD 50% now at 0.69530", "0.5"), ("Partially close AUDUSD now", "0.5"),
    ("Close AUDUSD partially at 0.69530 (+0.8%)", "0.5"), ("Close 1/2 of AUDUSD", "0.5"),
    ("Close 50 percent of the position", "0.5"), ("Secure 50% profit on XAUUSD", "0.5"),
    ("close 30% now", "0.3"), ("Close half", "0.5"),
])
def test_partial_close_wording_is_a_partial_close(text, fraction):
    """partial-close-read-as-full-close"""
    r = parse(text)
    assert actions(r) == [FollowUpAction.CLOSE_PARTIAL] and r.follow_ups[0].fraction == D(fraction)


def test_the_p_and_l_percent_of_the_real_close_is_not_a_partial_size():
    for text in (AUDUSD_CLOSE, "‼️ Close AUDUSD Manually now at 0.69530! (+43%)",
                 "Close AUDUSD now at 0.69530, 14% profit", "Close now, we are 14% up"):
        (fu,) = parse(text).follow_ups
        assert fu.action is FollowUpAction.CLOSE and fu.fraction is None


@pytest.mark.parametrize("text", ["Close AUDUSD 50% now at 0.69530", "Close 1/2 of AUDUSD",
                                  "Partially close AUDUSD now"])
def test_close_50_percent_closes_about_half_the_lots(text):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, AUDUSD_SIGNAL, 501, T0)
    (pos,) = bot_positions(fake)
    full = pos.volume
    post(p, text, 502, T0 + timedelta(minutes=10), reply_to=501)
    (closed,) = [r["volume"] for r in fake.sent if r["action"] == fake.TRADE_ACTION_DEAL and "position" in r]
    assert abs(closed - full / 2) <= 0.01 and bot_positions(fake) == [pos]


# ================================================= C15: an update is never a new trade

UPDATE_A = "XAUUSD Buy update\nTarget Profit 1 @ 4484.30 hit ✅\nStop Loss @ 4479.69 (moved to entry)"
UPDATE_B = "XAUUSD Buy running ✅ TP1 @ 4484.30 reached, move SL to BE"
CANCEL_QUOTING_ORDER = ("Cancel Pending Order on XAUUSD\nSell Limit Order @ 4156.56\nTarget Profit 1 @ 4153.58\n"
                        "Target Profit 2 @ 4142.80\nStop Loss @ 4162.55")
CLOSED_REPOST = XAUUSD_MARKET + "\n\nClosed manually at 4478.00 ❌"


def test_update_quoting_tp1_and_the_moved_sl_is_not_a_signal():
    """management-update-parsed-as-new-signal (A)"""
    r = parse(UPDATE_A)
    assert r.status is ParseStatus.UNCERTAIN and not r.signals and not r.follow_ups


def test_update_quoting_tp1_with_move_sl_to_be_is_the_be_command():
    """management-update-parsed-as-new-signal (B)"""
    r = parse(UPDATE_B)
    assert r.status is ParseStatus.FOLLOW_UP and not r.signals and actions(r) == [FollowUpAction.MOVE_SL_BE]
    r = parse("XAUUSD Buy\nTarget Profit 1 @ 4484.30 ✅\nUpdate the SL to the entry price")
    assert r.status is ParseStatus.UNCERTAIN and not r.signals  # a BE wording this parser does not know: reported


def test_update_quoting_tp1_and_sl_opens_no_second_trade():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4482.90, 4483.20)
    post(p, UPDATE_A, 506, T0 + timedelta(minutes=10), reply_to=505)
    assert len(fake.market_sends()) == 1 and bot_positions(fake) == [pos] and sig(p, 506) is None
    assert warned(p) and not any("Нов сигнал" in t for t in p.notifier.sent[1:])


def test_update_with_tp1_reached_and_move_sl_to_be_moves_the_stop():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4484.60, 4484.90)
    post(p, UPDATE_B, 506, T0 + timedelta(minutes=10), reply_to=505)
    assert len(fake.market_sends()) == 1 and pos.sl == pos.price_open == 4479.80
    assert not any("Отхвърлен" in t for t in p.notifier.sent)


@pytest.mark.parametrize("text, price", [
    ("XAUUSD Buy - New Stop Loss @ 4475.00", "4475.00"), ("XAUUSD Buy new SL 4475", "4475"),
    ("XAUUSD Buy move SL 4475", "4475"), ("Gold buy running +80 pips, new SL @ 4475", "4475"),
    ("XAUUSD Sell - New SL @ 4160.00", "4160.00"),
])
def test_sl_update_with_a_side_word_is_a_move_sl_not_a_signal(text, price):
    """update-msg-with-side-and-sl-opens-new-trade"""
    r = parse(text)
    assert not r.signals and actions(r) == [FollowUpAction.MOVE_SL] and r.follow_ups[0].price == D(price)


@pytest.mark.parametrize("text, reason", [
    ("XAUUSD Buy - Update the Stop Loss @ 4475.00", "update_with_levels"),  # no command this parser knows
    ("XAUUSD Buy close half\nStop Loss @ 4479.69", "update_with_levels"),  # which SL is meant?
    ("XAUUSD Buy move SL to BE\nTP2 @ 4510", "update_with_levels"),  # a new TP is not guessed
    ("XAUUSD Sell\nSL 2370", "incomplete_signal"),  # no entry line, no TP
])
def test_levels_next_to_a_side_word_without_an_entry_line_are_not_traded(text, reason):
    r = parse(text, reply_to=None)
    assert r.status is ParseStatus.UNCERTAIN and not r.signals and [i.reason for i in r.issues] == [reason]


def test_a_reply_that_repeats_levels_without_an_entry_line_is_not_a_new_trade():
    text = "XAUUSD Buy\nTarget Profit 1 @ 4484.30\nStop Loss @ 4479.69"
    assert [i.reason for i in parse(text, reply_to=505).issues] == ["reply_without_entry"]
    assert parse("GBPJPY SELL\nSL 191.20\nTP 190.40", reply_to=None).status is ParseStatus.SIGNAL  # not a reply


def test_new_stop_loss_reply_moves_the_stop_and_opens_nothing():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4480.90, 4481.20)
    post(p, "XAUUSD Buy - New Stop Loss @ 4475.00", 506, T0 + timedelta(minutes=10), reply_to=505)
    assert len(fake.market_sends()) == 1 and pos.sl == 4475.0


def test_new_stop_loss_for_a_trade_the_bot_never_saw_opens_nothing():
    fake = StrictMt5(demo=False)
    p = make(fake)
    fake.ticks["XAUUSD"] = (4480.90, 4481.20)
    post(p, "XAUUSD Buy - New Stop Loss @ 4475.00", 506, T0, reply_to=505)
    assert fake.market_sends() == [] and bot_positions(fake) == [] and sig(p, 506) is None


def test_new_sl_for_a_pending_sell_limit_sells_nothing_at_market():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    post(p, "XAUUSD Sell - New SL @ 4160.00", 801, T0 + timedelta(minutes=5), reply_to=800)
    assert fake.market_sends() == [] and bot_positions(fake) == [] and len(fake.orders) == 1


def test_cancel_that_quotes_the_order_places_no_second_order_and_notifies():
    r = parse(CANCEL_QUOTING_ORDER, reply_to=800)
    assert r.status is ParseStatus.UNCERTAIN and [i.reason for i in r.issues] == ["signal_with_result_text"]
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    post(p, CANCEL_QUOTING_ORDER, 801, T0 + timedelta(minutes=5), reply_to=800)
    assert len(fake.orders) == 1 and len([r for r in fake.sent if r["action"] == fake.TRADE_ACTION_PENDING]) == 1
    assert warned(p)
    # a quick edit of that post is still not a new order
    edit(p, CANCEL_QUOTING_ORDER + " ", 801, T0 + timedelta(minutes=5, seconds=20),
         posted=T0 + timedelta(minutes=5), reply_to=800)
    assert len([r for r in fake.sent if r["action"] == fake.TRADE_ACTION_PENDING]) == 1


def test_signal_reposted_with_closed_manually_is_not_traded_again():
    r = parse(CLOSED_REPOST, reply_to=None)
    assert r.status is ParseStatus.UNCERTAIN and [i.reason for i in r.issues] == ["signal_with_result_text"]
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    fake.ticks["XAUUSD"] = (4477.90, 4478.20)
    post(p, "‼️ Close XAUUSD Manually now at 4478.00! (-0.2%)", 506, T0 + timedelta(minutes=10), reply_to=505)
    assert bot_positions(fake) == []
    post(p, CLOSED_REPOST, 510, T0 + timedelta(minutes=20))
    assert bot_positions(fake) == [] and len(fake.market_sends()) == 1 and sig(p, 510)["status"] == "REJECTED"
    edit(p, CLOSED_REPOST + " ", 510, T0 + timedelta(minutes=20, seconds=20), posted=T0 + timedelta(minutes=20))
    assert bot_positions(fake) == [] and len(fake.market_sends()) == 1


def test_signal_reposted_with_its_tp1_marked_hit_is_not_traded_again():
    text = XAUUSD_MARKET.replace("Target Profit 1 @ 4484.30", "Target Profit 1 @ 4484.30 ✅")
    assert parse(text, reply_to=None).status is ParseStatus.UNCERTAIN
    assert parse(text, reply_to=None, edited=True).signals[0].tp == [D("4484.30"), D("4498.30")]  # edits still work


# ================================================= C15: an unknown order type is never market

@pytest.mark.parametrize("line", ["Buy-Stop Order @ 4490.00", "Buy (Stop Order) @ 4490.00", "Stop Buy Order @ 4490.00",
                                  "Buy Pending Stop Order @ 4490.00", "Buy: Stop Order @ 4490.00",
                                  "Buy, Stop Order @ 4490.00", "Buy Stop Order @ 4490.00"])
def test_buy_stop_spellings_are_a_buy_stop(line):
    """pending-order-phrasing-becomes-market"""
    r = parse(gold(line, "4500.00", "4510.00", "4470.00"), reply_to=None)
    (s,) = r.signals
    assert r.status is ParseStatus.SIGNAL and s.entry.type is EntryType.STOP and s.entry.price == D("4490.00")


@pytest.mark.parametrize("line", ["Buy Stp Order @ 4490.00", "Buy Sotp Order @ 4490.00", "Buy Stop Limit Order @ 4490.00",
                                  "Buy Order @ 4490.00"])
def test_an_order_type_the_parser_does_not_know_is_uncertain(line):
    r = parse(gold(line, "4500.00", "4510.00", "4470.00"), reply_to=None)
    assert r.status is ParseStatus.UNCERTAIN and not r.signals
    assert [i.reason for i in r.issues] == ["unclear_entry_type"]


def test_a_typo_in_the_real_sell_limit_is_uncertain_not_market():
    r = parse(gold("Sell Limt Order @ 4156.56", "4153.58", "4142.80", "4162.55"), reply_to=None)
    assert [i.reason for i in r.issues] == ["unclear_entry_type"]
    r = parse(gold("Sell-Limit Order @ 4156.56", "4153.58", "4142.80", "4162.55"), reply_to=None)
    assert r.signals[0].entry.type is EntryType.LIMIT


def test_buy_stop_spelled_with_a_hyphen_places_a_buy_stop():
    fake = StrictMt5(demo=False)  # ask 4479.80, 10.20 below the stop
    p = make(fake)
    post(p, gold("Buy-Stop Order @ 4490.00", "4500.00", "4510.00", "4470.00"), 600, T0)
    assert fake.market_sends() == [] and bot_positions(fake) == []
    assert [o.type for o in fake.orders.values()] == [fake.ORDER_TYPE_BUY_STOP]


@pytest.mark.parametrize("line, tp1, tp2, sl", [("Buy Stp Order @ 4490.00", "4500.00", "4510.00", "4470.00"),
                                                ("Sell Limt Order @ 4479.60", "4470.00", "4460.00", "4490.00")])
def test_a_misspelt_order_type_sends_nothing_and_notifies(line, tp1, tp2, sl):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, gold(line, tp1, tp2, sl), 600, T0)
    assert fake.sent == [] and warned(p)


# ================================================= C13: SL commands with a distance

@pytest.mark.parametrize("text", ["Move SL to entry +5 pips", "SL to BE+10", "Trail SL 20 pips",
                                  "Move SL to BE plus 5"])
def test_be_with_an_offset_or_a_trail_distance_is_uncertain(text):
    """move-sl-pips-read-as-price: the offset is a distance, not a price; nothing is guessed."""
    r = parse(text)
    assert r.status is ParseStatus.UNCERTAIN and not r.follow_ups


def test_be_with_a_profit_note_is_still_be():
    assert actions(parse("Move SL to BE (+80 pips in profit)")) == [FollowUpAction.MOVE_SL_BE]


# ================================================= a rules file without these protections is refused

def test_an_older_custom_rules_file_is_refused_at_start(tmp_path):
    import yaml

    from signal_copier.parser.rules import DEFAULT_RULES_PATH, ParserRules
    from signal_copier.settings import ConfigError

    data = yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    for key in ("not_commands", "command_guards", "command_words"):
        data.pop(key)
    old = tmp_path / "rules.yaml"
    old.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="not_commands"):
        ParserRules.load(old)
    ParserRules.load()  # the built-in file has all of them
