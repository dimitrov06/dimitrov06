"""End-to-end: Telegram message -> parser -> risk -> fake OANDA -> SQLite + notifications."""

import asyncio
from datetime import timedelta
from decimal import Decimal as D

from conftest import NOW
from signal_copier.models import IncomingMessage
from signal_copier.settings import Mode

AUDUSD_SIGNAL = """RTED Investing - Premium Trading Signals 🏆
AUDUSD
Buy now @ 0.69401
Target Profit 1 @ 0.69446
Target Profit 2 @ 0.69563
Stop Loss @ 0.69311"""
AUDUSD_CLOSE = "‼️ Close AUDUSD Manually now at 0.69530! (+1.43%)"


def msg(text, message_id, reply_to=None, edited=False, date=NOW):
    return IncomingMessage(chat_id=-1001, message_id=message_id, text=text, date=date,
                           reply_to_msg_id=reply_to, edited=edited)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ record mode


def test_record_mode_stores_signal_and_close_without_trading(make_pipeline, fake):
    p = make_pipeline(Mode.RECORD)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg(AUDUSD_CLOSE, 502, reply_to=501)))

    sig = p.storage.get_signal("-1001:501:0")
    assert sig["status"] == "RECORDED" and sig["symbol"] == "AUD_USD"
    fu = p.storage.get_follow_up("-1001:502:CLOSE")
    assert fu["status"] == "RECORDED" and fu["signal_key"] == "-1001:501:0" and fu["price"] == "0.69530"
    assert fake.requests == []  # never touched OANDA
    assert any("Нов сигнал" in t for t in p.notifier.sent)
    assert any("Записано" in t for t in p.notifier.sent)


def test_uncertain_message_is_not_traded_and_notifies(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg("PEPEUSDT BUY NOW\nSL 0.0001\nTP 0.0002", 600)))
    assert fake.market_orders() == []
    assert "НЕ търгувам" in p.notifier.sent[-1]
    assert p.storage.events("parse")[-1]["decision"] == "UNCERTAIN"


# ------------------------------------------------------------------- paper mode


def test_paper_opens_one_trade_per_tp(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))

    posts = fake.market_orders()
    assert len(posts) == 2
    o1, o2 = posts[0][2]["order"], posts[1][2]["order"]
    assert o1["type"] == "MARKET" and o1["timeInForce"] == "FOK"
    assert o1["stopLossOnFill"]["price"] == "0.69311"
    assert (o1["takeProfitOnFill"]["price"], o2["takeProfitOnFill"]["price"]) == ("0.69446", "0.69563")
    # 0.5% of 10000 = 50 USD over 0.00091 SL distance (fill at ask 0.69402) -> 54945 units
    assert int(o1["units"]) + int(o2["units"]) == 54945
    assert p.storage.get_signal("-1001:501:0")["status"] == "EXECUTED"
    assert len(p.storage.trades_for_signal("-1001:501:0")) == 2
    assert any("Отворена" in t for t in p.notifier.sent)


def test_duplicate_delivery_and_restart_never_double_order(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))  # same message delivered again
    assert len(fake.market_orders()) == 2
    # simulate the pipeline seeing the same signal after a restart (message row exists, signal executed)
    from signal_copier.parser import SignalParser

    s = SignalParser().parse(msg(AUDUSD_SIGNAL, 501)).signals[0]
    run(p.handle_signal(s))
    assert len(fake.market_orders()) == 2


def test_close_reply_closes_all_trades_and_edit_does_not_close_twice(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg(AUDUSD_CLOSE, 502, reply_to=501)))
    closes = [r for r in fake.requests if r[0] == "PUT" and r[1].endswith("/close")]
    assert len(closes) == 2
    assert p.storage.get_follow_up("-1001:502:CLOSE")["status"] == "EXECUTED"

    run(p.handle_message(msg(AUDUSD_CLOSE + " ✅", 502, reply_to=501, edited=True)))  # channel edits the message
    assert len([r for r in fake.requests if r[0] == "PUT" and r[1].endswith("/close")]) == 2

    run(p.monitor_once())  # monitor notices the trades are closed
    assert p.storage.open_trades() == []
    assert any("Затворена" in t for t in p.notifier.sent)


def test_move_sl_to_be_and_close_half(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg("TP1 hit ✅ move SL to BE", 503, reply_to=501)))
    sl_updates = [r[2]["stopLoss"]["price"] for r in fake.requests if r[1].endswith("/orders") and r[0] == "PUT"]
    assert sl_updates == ["0.69402", "0.69402"]  # actual fill price of each trade

    run(p.handle_message(msg("Close half", 504, reply_to=501)))
    partial = [r[2]["units"] for r in fake.requests if r[1].endswith("/close")]
    assert len(partial) == 2 and all(u != "ALL" for u in partial)


def test_follow_up_without_target_is_not_executed(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg("close all", 700)))
    assert p.storage.get_follow_up("-1001:700:CLOSE")["reason"] == "no_reply_and_no_symbol"
    assert not [r for r in fake.requests if r[0] == "PUT"]


def test_follow_up_by_symbol_finds_recent_signal(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg("AUDUSD close now", 505)))
    assert p.storage.get_follow_up("-1001:505:CLOSE")["signal_key"] == "-1001:501:0"


def test_stop_command_blocks_new_trades_but_resume_allows(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    assert "Спрях" in run(p.handle_command("/stop"))
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    assert fake.market_orders() == []
    assert p.storage.get_signal("-1001:501:0")["reason"] == "halted:manual_stop"
    status = run(p.handle_command("/status"))
    assert "спрян" in status and "paper" in status

    run(p.handle_command("/resume"))
    run(p.handle_message(msg(AUDUSD_SIGNAL.replace("AUDUSD", "AUDUSD "), 510)))
    assert len(fake.market_orders()) == 2


def test_old_signal_is_rejected(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER, clock_offset_s=600)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    assert fake.market_orders() == []
    assert p.storage.get_signal("-1001:501:0")["reason"].startswith("signal_too_old")


def test_daily_loss_limit_halts_until_next_day(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.monitor_once())  # records today's starting NAV = 10000
    fake.nav = D("9790")  # -2.1%
    run(p.monitor_once())
    assert p.halted_reason() == "daily_loss_limit"
    assert any("дневен лимит" in t for t in p.notifier.sent)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    assert fake.market_orders() == []


def test_edited_signal_moves_sl_of_open_trades(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    edited = AUDUSD_SIGNAL.replace("0.69311", "0.69330")
    run(p.handle_message(msg(edited, 501, edited=True, date=NOW + timedelta(seconds=2))))
    sl_updates = [r[2]["stopLoss"]["price"] for r in fake.requests if r[1].endswith("/orders") and r[0] == "PUT"]
    assert sl_updates == ["0.69330", "0.69330"]
    assert len(fake.market_orders()) == 2  # no new orders
    assert p.storage.get_signal("-1001:501:0")["sl"] == "0.69330"


def test_rejected_signal_is_reevaluated_after_edit_adds_sl(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    no_sl = AUDUSD_SIGNAL.replace("\nStop Loss @ 0.69311", "")
    run(p.handle_message(msg(no_sl, 520)))
    assert p.storage.get_signal("-1001:520:0")["reason"] == "missing_sl"
    run(p.handle_message(msg(AUDUSD_SIGNAL, 520, edited=True, date=NOW + timedelta(seconds=3))))
    assert len(fake.market_orders()) == 2


def test_max_positions_per_symbol(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER, max_positions_per_symbol=1)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg(AUDUSD_SIGNAL, 530)))
    assert p.storage.get_signal("-1001:530:0")["reason"] == "max_positions_per_symbol"


def test_every_decision_is_logged_as_event(make_pipeline, fake):
    p = make_pipeline(Mode.PAPER)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg("Good morning traders ☀️", 540)))
    kinds = [(e["kind"], e["decision"]) for e in p.storage.events()]
    assert ("risk", "APPROVED") in kinds and ("execute", "EXECUTED") in kinds and ("parse", "IGNORED") in kinds
