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
