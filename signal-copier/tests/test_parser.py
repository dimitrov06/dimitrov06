"""Parser tests.

NOTE: the sample messages below are INVENTED placeholders in typical signal-group
style. Replace / extend them with real messages from the channel.
"""

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from signal_copier.models import EntryType, FollowUpAction, IncomingMessage, ParseStatus, Side
from signal_copier.parser import SignalParser

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def parser():
    return SignalParser()


def msg(text, message_id=100, reply_to=None, edited=False):
    return IncomingMessage(
        chat_id=-1001, message_id=message_id, text=text, date=NOW, reply_to_msg_id=reply_to, edited=edited
    )


# --------------------------------------------------------------- signals


def test_gold_market_with_three_tps(parser):
    r = parser.parse(msg("🔥🔥 XAUUSD BUY NOW @ 2345.50 🔥🔥\n\n🛑 SL: 2335\n✅ TP1: 2350\n✅ TP2: 2355\n✅ TP3: 2365"))
    assert r.status is ParseStatus.SIGNAL
    s = r.signals[0]
    assert (s.symbol, s.raw_symbol, s.side) == ("XAU_USD", "XAUUSD", Side.BUY)
    assert s.entry.type is EntryType.MARKET and s.entry.price == D("2345.50")
    assert s.sl == D("2335")
    assert s.tp == [D("2350"), D("2355"), D("2365")]
    assert s.message_id == 100 and s.key == "-1001:100:0"


def test_gold_alias_sell_zone_shorthand_and_tp_list(parser):
    s = parser.parse(msg("GOLD SELL 2350-53\nSL 2358\nTP 2345 / 2340 / 2330")).signals[0]
    assert s.symbol == "XAU_USD" and s.side is Side.SELL
    assert (s.entry.zone_low, s.entry.zone_high) == (D("2350"), D("2353"))
    assert s.entry.reference_price == D("2351.5")
    assert s.tp == [D("2345"), D("2340"), D("2330")]


def test_buy_limit_fx_with_slash_pair(parser):
    s = parser.parse(msg("EUR/USD Buy Limit 1.0850\nStop Loss 1.0820\nTake Profit 1.0900")).signals[0]
    assert s.symbol == "EUR_USD"
    assert s.entry.type is EntryType.LIMIT and s.entry.price == D("1.0850")
    assert s.sl == D("1.0820") and s.tp == [D("1.0900")]


def test_index_with_digits_in_symbol(parser):
    s = parser.parse(msg("US30 sell now\nentry 39250\nsl 39350\ntp1 39150\ntp2 39000")).signals[0]
    assert s.symbol == "US30_USD"
    assert s.entry.price == D("39250")
    assert s.tp == [D("39150"), D("39000")]


def test_single_line_signal(parser):
    s = parser.parse(msg("NAS100 BUY 19850 SL 19800 TP 19900 TP 19950")).signals[0]
    assert s.symbol == "NAS100_USD" and s.entry.price == D("19850")
    assert s.sl == D("19800") and s.tp == [D("19900"), D("19950")]


def test_market_without_price(parser):
    s = parser.parse(msg("GBPJPY SELL\nSL 191.20\nTP 190.40")).signals[0]
    assert s.entry.type is EntryType.MARKET and s.entry.reference_price is None
    assert s.symbol == "GBP_JPY"


def test_thousands_separator_and_tp_noise(parser):
    s = parser.parse(msg("US30 BUY @ 39,250\nSL 39,150 (100 pts)\nTP1 39,350 (+100 pts) RR 1:1")).signals[0]
    assert s.entry.price == D("39250") and s.sl == D("39150") and s.tp == [D("39350")]


def test_bare_price_can_be_configured_as_limit():
    p = SignalParser(bare_price_entry="limit")
    s = p.parse(msg("XAUUSD BUY 2345\nSL 2335\nTP 2355")).signals[0]
    assert s.entry.type is EntryType.LIMIT
    s = p.parse(msg("XAUUSD BUY NOW 2345\nSL 2335\nTP 2355")).signals[0]
    assert s.entry.type is EntryType.MARKET


def test_several_signals_in_one_message(parser):
    r = parser.parse(
        msg("📊 TODAY'S SIGNALS\nEURUSD BUY 1.0850\nSL 1.0820\nTP 1.0900\n\nXAUUSD SELL 2360\nSL 2370\nTP 2345")
    )
    assert r.status is ParseStatus.SIGNAL
    assert [s.symbol for s in r.signals] == ["EUR_USD", "XAU_USD"]
    assert [s.index for s in r.signals] == [0, 1]
    assert r.signals[1].side is Side.SELL and r.signals[1].sl == D("2370")


# --------------------------------------------------------------- edge cases


def test_missing_sl_is_parsed_but_flagged(parser):
    r = parser.parse(msg("XAUUSD BUY NOW 2345\nTP 2355"))
    s = r.signals[0]
    assert s.sl is None and "missing_sl" in s.warnings  # risk/ rejects it


def test_unknown_symbol_is_uncertain(parser):
    r = parser.parse(msg("PEPEUSDT BUY NOW\nSL 0.0001\nTP 0.0002"))
    assert r.status is ParseStatus.UNCERTAIN
    assert r.issues[0].reason == "unknown_symbol"
    assert not r.signals


def test_sl_on_wrong_side_is_uncertain(parser):
    r = parser.parse(msg("XAUUSD BUY 2345\nSL 2355\nTP 2360"))
    assert r.status is ParseStatus.UNCERTAIN and r.issues[0].reason == "sl_wrong_side_of_entry"


def test_typo_price_out_of_range_is_uncertain(parser):
    r = parser.parse(msg("XAUUSD BUY 2345\nSL 2335\nTP 23500"))
    assert r.status is ParseStatus.UNCERTAIN and r.issues[0].reason == "price_out_of_range"


def test_conflicting_sides_is_uncertain(parser):
    r = parser.parse(msg("XAUUSD BUY or SELL 2345\nSL 2335\nTP 2355"))
    assert r.status is ParseStatus.UNCERTAIN


def test_signal_without_side_is_uncertain(parser):
    r = parser.parse(msg("XAUUSD 2345\nSL 2335\nTP 2355"))
    assert r.status is ParseStatus.UNCERTAIN and r.issues[0].reason == "missing_side"


def test_pending_without_price_is_uncertain(parser):
    r = parser.parse(msg("EURUSD SELL LIMIT\nSL 1.0900\nTP 1.0800"))
    assert r.issues[0].reason == "pending_order_without_price"


def test_chatter_is_ignored(parser):
    for text in ["Good morning traders ☀️", "GOLD is flying today 🚀🚀", "Weekly result: +450 pips 💰"]:
        assert parser.parse(msg(text)).status is ParseStatus.IGNORED


def test_edited_message_keeps_id_and_flag(parser):
    original = parser.parse(msg("XAUUSD BUY NOW\nSL 2335\nTP 2350", message_id=7))
    edited = parser.parse(msg("XAUUSD BUY NOW\nSL 2330\nTP 2350", message_id=7, edited=True))
    assert original.signals[0].key == edited.signals[0].key
    assert edited.signals[0].edited and edited.signals[0].sl == D("2330")


def test_edit_can_turn_incomplete_message_into_signal(parser):
    assert parser.parse(msg("XAUUSD BUY NOW", message_id=8)).signals[0].sl is None
    fixed = parser.parse(msg("XAUUSD BUY NOW\nSL 2335\nTP 2350", message_id=8, edited=True))
    assert fixed.signals[0].sl == D("2335")


def test_emoji_heavy_formatting(parser):
    s = parser.parse(msg("🟢🟢 *GOLD* 🟢🟢\n➡️ **BUY** NOW\n⛔️SL➖2335\n🎯TP➖2350")).signals[0]
    assert s.symbol == "XAU_USD" and s.side is Side.BUY
    assert s.sl == D("2335") and s.tp == [D("2350")]


# --------------------------------------------------------------- follow-ups


@pytest.mark.parametrize(
    "text, action",
    [
        ("Move SL to BE", FollowUpAction.MOVE_SL_BE),
        ("SL to breakeven guys 🔒", FollowUpAction.MOVE_SL_BE),
        ("set stop loss to entry", FollowUpAction.MOVE_SL_BE),
        ("Close now ❌", FollowUpAction.CLOSE),
        ("close", FollowUpAction.CLOSE),
        ("Close half and let the rest run", FollowUpAction.CLOSE_PARTIAL),
        ("Cancel the limit order", FollowUpAction.CANCEL),
        ("Delete pending", FollowUpAction.CANCEL),
        ("SL hit 😔", FollowUpAction.SL_HIT),
    ],
)
def test_follow_up_commands(parser, text, action):
    r = parser.parse(msg(text, message_id=200, reply_to=100))
    assert r.status is ParseStatus.FOLLOW_UP
    assert [f.action for f in r.follow_ups] == [action]
    assert r.follow_ups[0].reply_to_msg_id == 100


def test_close_half_fraction(parser):
    fu = parser.parse(msg("Close half", reply_to=100)).follow_ups[0]
    assert fu.fraction == D("0.5")
    fu = parser.parse(msg("close 30% now", reply_to=100)).follow_ups[0]
    assert fu.action is FollowUpAction.CLOSE_PARTIAL and fu.fraction == D("0.3")
    fu = parser.parse(msg("close 100%", reply_to=100)).follow_ups[0]
    assert fu.action is FollowUpAction.CLOSE


def test_tp_hit_plus_move_sl(parser):
    r = parser.parse(msg("TP1 hit ✅ +50 pips 💰 move SL to BE", reply_to=100))
    actions = {f.action: f for f in r.follow_ups}
    assert actions[FollowUpAction.TP_HIT].tp_index == 1
    assert FollowUpAction.MOVE_SL_BE in actions
    assert r.status is ParseStatus.FOLLOW_UP


def test_tp_hit_emoji_only(parser):
    fu = parser.parse(msg("TP2 ✅✅", reply_to=100)).follow_ups[0]
    assert fu.action is FollowUpAction.TP_HIT and fu.tp_index == 2


def test_move_sl_to_price(parser):
    fu = parser.parse(msg("Move SL to 2348", reply_to=100)).follow_ups[0]
    assert fu.action is FollowUpAction.MOVE_SL and fu.price == D("2348")


def test_follow_up_by_symbol_without_reply(parser):
    fu = parser.parse(msg("GOLD close now")).follow_ups[0]
    assert fu.action is FollowUpAction.CLOSE and fu.symbol == "XAU_USD" and fu.has_target


def test_follow_up_without_any_target(parser):
    fu = parser.parse(msg("close all")).follow_ups[0]
    assert not fu.has_target  # resolver must not guess -> notify


def test_running_trade_update_is_not_a_new_signal(parser):
    r = parser.parse(msg("Gold buy running +80 pips 🔥 move SL to entry", reply_to=100))
    assert not r.signals
    assert r.follow_ups[0].action is FollowUpAction.MOVE_SL_BE


def test_follow_up_and_new_signal_in_one_message(parser):
    r = parser.parse(msg("GOLD TP1 hit ✅\nNew signal:\nEURUSD SELL 1.0900\nSL 1.0930\nTP 1.0850"))
    assert r.follow_ups[0].action is FollowUpAction.TP_HIT and r.follow_ups[0].symbol == "XAU_USD"
    assert r.signals[0].symbol == "EUR_USD"


def test_management_text_inside_signal_is_only_a_warning(parser):
    s = parser.parse(msg("XAUUSD BUY NOW\nSL 2335\nTP1 2350\nTP2 2360\nClose half at TP1 and move SL to BE")).signals[0]
    assert "contains_management_text" in s.warnings
    assert s.tp == [D("2350"), D("2360")]
