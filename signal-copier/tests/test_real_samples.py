"""Real messages from the signal channel (RTED Investing - Premium Trading Signals).

Add every new real format here; these take priority over the invented samples in test_parser.py.
"""

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from signal_copier.models import EntryType, FollowUpAction, IncomingMessage, ParseStatus, Side
from signal_copier.parser import SignalParser

AUDUSD_SIGNAL = """RTED Investing - Premium Trading Signals 🏆
AUDUSD
Buy now @ 0.69401
Target Profit 1 @ 0.69446
Target Profit 2 @ 0.69563
Stop Loss @ 0.69311"""

AUDCAD_SIGNAL = """RTED Investing - Premium Trading Signals 🏆
AUDCAD
Sell now @ 0.99541
Target Profit 1 @ 0.99359
Target Profit 2 @ 0.99179
Stop Loss @ 0.99723"""

XAUUSD_SIGNAL = """RTED Investing - Premium Trading Signals 🏆
XAUUSD
Buy Market Order @ 4479.69
Target Profit 1 @ 4484.30
Target Profit 2 @ 4498.30
Stop Loss @ 4470.38"""

AUDUSD_CLOSE = "‼️ Close AUDUSD Manually now at 0.69530! (+1.43%)"


@pytest.fixture(scope="module")
def parser():
    return SignalParser()


def msg(text, message_id, reply_to=None, edited=False):
    return IncomingMessage(
        chat_id=-1001,
        message_id=message_id,
        text=text,
        date=datetime(2026, 10, 8, 14, 14, tzinfo=timezone.utc),
        reply_to_msg_id=reply_to,
        edited=edited,
    )


def test_rted_audusd_signal(parser):
    r = parser.parse(msg(AUDUSD_SIGNAL, 501))
    assert r.status is ParseStatus.SIGNAL
    s = r.signals[0]
    assert (s.symbol, s.side) == ("AUD_USD", Side.BUY)
    assert s.entry.type is EntryType.MARKET and s.entry.price == D("0.69401")
    assert s.sl == D("0.69311")
    assert s.tp == [D("0.69446"), D("0.69563")]
    assert s.warnings == []


def test_rted_manual_close_reply(parser):
    r = parser.parse(msg(AUDUSD_CLOSE, 502, reply_to=501))
    assert r.status is ParseStatus.FOLLOW_UP
    fu = r.follow_ups[0]
    assert fu.action is FollowUpAction.CLOSE
    assert fu.reply_to_msg_id == 501 and fu.symbol == "AUD_USD"
    assert fu.price == D("0.69530")


def test_rted_edited_close_is_same_command(parser):
    # The channel edited the close message later; the executor must treat it as the same command (no double close).
    a = parser.parse(msg(AUDUSD_CLOSE, 502, reply_to=501)).follow_ups[0]
    b = parser.parse(msg(AUDUSD_CLOSE, 502, reply_to=501, edited=True)).follow_ups[0]
    assert (a.message_id, a.action) == (b.message_id, b.action) and b.edited


def test_rted_style_sell_variant(parser):
    # Same template, mirrored for SELL (invented values).
    text = AUDUSD_SIGNAL.replace("Buy", "Sell").replace("0.69446", "0.69356").replace("0.69563", "0.69239").replace(
        "0.69311", "0.69491"
    )
    s = parser.parse(msg(text, 503)).signals[0]
    assert s.side is Side.SELL and s.tp == [D("0.69356"), D("0.69239")] and s.sl == D("0.69491")


def test_rted_audcad_sell_signal(parser):
    r = parser.parse(msg(AUDCAD_SIGNAL, 504))
    assert r.status is ParseStatus.SIGNAL
    s = r.signals[0]
    assert (s.symbol, s.side) == ("AUD_CAD", Side.SELL)
    assert s.entry.type is EntryType.MARKET and s.entry.price == D("0.99541")
    assert s.sl == D("0.99723")
    assert s.tp == [D("0.99359"), D("0.99179")]
    assert s.warnings == []


def test_rted_xauusd_buy_market_order(parser):
    r = parser.parse(msg(XAUUSD_SIGNAL, 505))
    assert r.status is ParseStatus.SIGNAL
    s = r.signals[0]
    assert (s.symbol, s.side) == ("XAU_USD", Side.BUY)
    assert s.entry.type is EntryType.MARKET and s.entry.price == D("4479.69")
    assert s.sl == D("4470.38")
    assert s.tp == [D("4484.30"), D("4498.30")]
    assert s.warnings == []


# Entry formats from the RTED "Trading Signals Instructions" manual.
@pytest.mark.parametrize("text, symbol, side, etype, price", [
    ("RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nBuy Limit Order @ 4460.00\n"
     "Target Profit 1 @ 4470.00\nTarget Profit 2 @ 4490.00\nStop Loss @ 4450.00",
     "XAU_USD", Side.BUY, EntryType.LIMIT, "4460.00"),
    ("RTED Investing - Premium Trading Signals 🏆\nNASDAQ\nSell Stop Order @ 18,000\n"
     "Target Profit 1 @ 17,900\nTarget Profit 2 @ 17,750\nStop Loss @ 18,100",
     "NAS100_USD", Side.SELL, EntryType.STOP, "18000"),
    ("RTED Investing - Premium Trading Signals 🏆\nBTCUSD\nBuy Stop Order @ 65,000\n"
     "Target Profit 1 @ 66,000\nTarget Profit 2 @ 68,000\nStop Loss @ 63,500",
     "BTC_USD", Side.BUY, EntryType.STOP, "65000"),
    ("RTED Investing - Premium Trading Signals 🏆\nEURUSD\nSell Limit Order @ 1.08500\n"
     "Target Profit 1 @ 1.08300\nTarget Profit 2 @ 1.08000\nStop Loss @ 1.08700",
     "EUR_USD", Side.SELL, EntryType.LIMIT, "1.08500"),
])
def test_rted_manual_entry_formats(parser, text, symbol, side, etype, price):
    r = parser.parse(msg(text, 700))
    assert r.status is ParseStatus.SIGNAL, r.issues
    s = r.signals[0]
    assert (s.symbol, s.side, s.entry.type) == (symbol, side, etype)
    assert s.entry.price == D(price) and len(s.tp) == 2 and s.sl is not None


def test_rted_xauusd_sell_limit_2026_10_15(parser):
    text = ("RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nSell Limit Order @ 4156.56\n"
            "Target Profit 1 @ 4153.58\nTarget Profit 2 @ 4142.80\nStop Loss @ 4162.55")
    r = parser.parse(msg(text, 800))
    assert r.status is ParseStatus.SIGNAL
    s = r.signals[0]
    assert (s.symbol, s.side, s.entry.type, s.entry.price) == ("XAU_USD", Side.SELL, EntryType.LIMIT, D("4156.56"))
    assert s.sl == D("4162.55") and s.tp == [D("4153.58"), D("4142.80")] and not s.warnings


def test_rted_cancel_pending_order_2026_10_16(parser):
    r = parser.parse(msg("Cancel Pending Order on XAUUSD", 1523, reply_to=1522))
    assert r.status is ParseStatus.FOLLOW_UP
    fu = r.follow_ups[0]
    assert (fu.action, fu.symbol, fu.reply_to_msg_id) == (FollowUpAction.CANCEL, "XAU_USD", 1522)


# The parser reads commands only in command form (C14) and never takes an update for a new trade (C15).
# These guards keep the real formats exactly as they were.

@pytest.mark.parametrize("text", [AUDUSD_SIGNAL, AUDCAD_SIGNAL, XAUUSD_SIGNAL,
                                  "RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nSell Limit Order @ 4156.56\n"
                                  "Target Profit 1 @ 4153.58\nTarget Profit 2 @ 4142.80\nStop Loss @ 4162.55"])
def test_rted_signals_carry_no_command_and_no_result(parser, text):
    r = parser.parse(msg(text, 900))
    assert r.status is ParseStatus.SIGNAL and not r.follow_ups and not r.issues
    assert r.signals[0].management == [] and r.signals[0].warnings == []


@pytest.mark.parametrize("text, action, price", [
    (AUDUSD_CLOSE, FollowUpAction.CLOSE, "0.69530"),
    ("‼️ Close XAUUSD Manually now at 4485.10! (+0.50%)", FollowUpAction.CLOSE, "4485.10"),
    ("‼️ Close XAUUSD Manually now at 4478.00! (-0.2%)", FollowUpAction.CLOSE, "4478.00"),
    ("Cancel Pending Order on XAUUSD", FollowUpAction.CANCEL, None),
])
def test_rted_commands_are_exactly_one_command(parser, text, action, price):
    r = parser.parse(msg(text, 901, reply_to=900))
    assert r.status is ParseStatus.FOLLOW_UP and [f.action for f in r.follow_ups] == [action]
    assert r.follow_ups[0].fraction is None and r.follow_ups[0].price == (D(price) if price else None)


def test_rted_sell_limit_with_a_typo_in_the_order_type_is_not_taken_at_market(parser):
    """The channel types by hand ("Bu now" was seen): "Sell Limt Order" must never become a market sell."""
    text = ("RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nSell Limt Order @ 4156.56\n"
            "Target Profit 1 @ 4153.58\nTarget Profit 2 @ 4142.80\nStop Loss @ 4162.55")
    r = parser.parse(msg(text, 902))
    assert r.status is ParseStatus.UNCERTAIN and not r.signals
