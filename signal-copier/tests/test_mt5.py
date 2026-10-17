"""IC Markets / MT5 path, end-to-end against a fake MetaTrader5 module."""

import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pytest

from conftest import NOW
from fake_mt5 import FakeMt5
from signal_copier.executor.mt5 import Mt5Client, Mt5Executor, short_tag
from signal_copier.models import IncomingMessage
from signal_copier.notifier import Notifier
from signal_copier.parser import SignalParser
from signal_copier.pipeline import Pipeline
from signal_copier.risk import RiskManager
from signal_copier.settings import ConfigError, Mt5Cfg, Mt5Credentials, Settings
from signal_copier.storage import Storage

AUDUSD_SIGNAL = """RTED Investing - Premium Trading Signals 🏆
AUDUSD
Buy now @ 0.69401
Target Profit 1 @ 0.69446
Target Profit 2 @ 0.69563
Stop Loss @ 0.69311"""
XAUUSD_SIGNAL = """RTED Investing - Premium Trading Signals 🏆
XAUUSD
Buy Market Order @ 4479.69
Target Profit 1 @ 4484.30
Target Profit 2 @ 4498.30
Stop Loss @ 4470.38"""
AUDUSD_CLOSE = "‼️ Close AUDUSD Manually now at 0.69530! (+1.43%)"


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_):
    return None


def msg(text, message_id, reply_to=None, edited=False):
    return IncomingMessage(chat_id=-1001, message_id=message_id, text=text, date=NOW, reply_to_msg_id=reply_to,
                           edited=edited)


def make(fake: FakeMt5, demo_expected=True, tp_mode="equal"):
    settings = Settings.model_validate({"mode": "paper" if demo_expected else "live"})
    creds = Mt5Credentials(login=5100123, password="pw", server="ICMarketsSC-Demo", expect_demo=demo_expected)
    client = Mt5Client(creds, Mt5Cfg(), mt5_module=fake)
    client.sleep = _no_sleep
    storage = Storage(":memory:")
    executor = Mt5Executor(client, storage, retry_attempts=3, retry_base_delay_s=0, tp_mode=tp_mode)
    p = Pipeline(settings, storage, SignalParser(), Notifier(None, None), RiskManager(settings.risk), client,
                 executor, clock=lambda: NOW + timedelta(seconds=5))
    return p, client


def test_symbol_mapping():
    _, client = make(FakeMt5())
    assert client.symbol("XAU_USD") == "XAUUSD"
    assert client.symbol("US30_USD") == "US30"
    assert client.symbol("NAS100_USD") == "USTEC"
    client.cfg = Mt5Cfg(symbol_suffix=".a")
    assert client.symbol("EUR_USD") == "EURUSD.a"


def test_connect_refuses_real_account_in_paper_mode():
    _, client = make(FakeMt5(demo=False), demo_expected=True)
    with pytest.raises(ConfigError, match="real"):
        run(client.connect())
    _, client = make(FakeMt5(demo=True), demo_expected=False)
    with pytest.raises(ConfigError, match="demo"):
        run(client.connect())


def test_audusd_signal_opens_two_positions_sized_in_lots():
    fake = FakeMt5()
    p, _ = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    sends = fake.market_sends()
    assert len(sends) == 2
    # 50 USD risk / (0.00091 * 100000 USD per lot) = 0.549 -> 0.54 lots -> 0.27 + 0.27
    assert [r["volume"] for r in sends] == [0.27, 0.27]
    assert [r["tp"] for r in sends] == [0.69446, 0.69563]
    assert all(r["sl"] == 0.69311 and r["symbol"] == "AUDUSD" and r["price"] == 0.69402 for r in sends)
    assert sends[0]["comment"] == short_tag("sc_n1001_501_0_0") and len(sends[0]["comment"]) <= 31
    assert len(fake.positions) == 2
    assert p.storage.get_signal("-1001:501:0")["status"] == "EXECUTED"


def test_gold_lot_size():
    fake = FakeMt5()
    p, _ = make(fake)
    run(p.handle_message(msg(XAUUSD_SIGNAL, 502)))
    # 50 USD / (9.42 * 100 USD per lot) = 0.053 -> 0.05 lots -> 0.02 + 0.03
    assert [r["volume"] for r in fake.market_sends()] == [0.02, 0.03]


def test_lost_result_is_found_by_comment_not_resent():
    fake = FakeMt5()
    fake.script = ["lost"]  # first order executes but the terminal returns None
    p, _ = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    assert len(fake.positions) == 2 and len(fake.market_sends()) == 2
    assert all(o["status"] == "FILLED" for o in p.storage.orders_for_signal("-1001:501:0"))


def test_requote_is_retried_with_fresh_price():
    fake = FakeMt5()
    fake.script = ["requote"]
    p, _ = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    assert len(fake.positions) == 2 and len(fake.market_sends()) == 3


def test_timeout_is_never_resent():
    """A TIMEOUT can still fill later: the part is looked for, never sent again, and left SENT for sync()."""
    fake = FakeMt5()
    fake.script = ["timeout"]
    p, _ = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    assert len(fake.positions) == 1 and len(fake.market_sends()) == 2
    assert [o["status"] for o in p.storage.orders_for_signal("-1001:501:0")] == ["SENT", "FILLED"]
    assert p.storage.get_signal("-1001:501:0")["status"] == "EXECUTED"
    assert any("НЕ я пращам повторно" in t for t in p.notifier.sent)


def test_close_reply_and_edit_do_not_double_close():
    fake = FakeMt5()
    p, _ = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg(AUDUSD_CLOSE, 502, reply_to=501)))
    assert fake.positions == {}
    run(p.handle_message(msg(AUDUSD_CLOSE + " ✅", 502, reply_to=501, edited=True)))
    closes = [r for r in fake.sent if "position" in r and r["action"] == fake.TRADE_ACTION_DEAL]
    assert len(closes) == 2
    run(p.monitor_once())
    assert p.storage.open_trades() == []
    assert any("Затворена" in t for t in p.notifier.sent)


def test_move_sl_to_be_and_close_half():
    fake = FakeMt5()
    p, _ = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    fake.ticks["AUDUSD"] = (0.69450, 0.69452)  # TP1 0.69446 reached
    run(p.handle_message(msg("TP1 hit ✅ move SL to BE", 503, reply_to=501)))
    assert all(pos.sl == 0.69402 for pos in fake.positions.values())
    run(p.handle_message(msg("Close half", 504, reply_to=501)))
    assert sorted(pos.volume for pos in fake.positions.values()) == [0.14, 0.14]  # 0.27 - 0.13


def test_limit_order_fill_and_tp_detected_by_monitor():
    fake = FakeMt5()
    p, _ = make(fake)
    run(p.handle_message(msg("XAUUSD BUY LIMIT 4475\nSL 4465\nTP 4495", 600)))
    assert len(fake.orders) == 1
    order_ticket = next(iter(fake.orders))
    pos_ticket = fake.fill_pending(order_ticket)
    run(p.monitor_once())
    assert p.storage.trades_for_signal("-1001:600:0")[0]["trade_id"] == str(pos_ticket)
    fake.hit_tp(pos_ticket, 4495.0)
    run(p.monitor_once())
    closed = p.storage.trades_for_signal("-1001:600:0", open_only=False)[0]
    assert closed["state"] == "CLOSED" and D(closed["realized_pl"]) == D("43.6")  # 45 - 0.7 in - 0.7 out


def test_cancel_pending():
    fake = FakeMt5()
    p, _ = make(fake)
    run(p.handle_message(msg("XAUUSD BUY LIMIT 4475\nSL 4465\nTP 4495", 600)))
    run(p.handle_message(msg("Cancel the limit order", 601, reply_to=600)))
    assert fake.orders == {}


def test_daily_loss_limit_uses_equity():
    fake = FakeMt5()
    p, _ = make(fake)
    run(p.monitor_once())
    fake.equity = 9790.0
    run(p.monitor_once())
    assert p.halted_reason() == "daily_loss_limit"


def test_rted_mode_one_position_with_tp2():
    """RTED manual: one trade, TP2 is the platform take-profit, TP1 is only an observation level."""
    fake = FakeMt5()
    p, _ = make(fake, tp_mode="last")
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    sends = fake.market_sends()
    assert len(sends) == 1
    assert sends[0]["volume"] == 0.54 and sends[0]["tp"] == 0.69563 and sends[0]["sl"] == 0.69311
    fake.ticks["AUDUSD"] = (0.69450, 0.69452)  # TP1 0.69446 reached
    run(p.handle_message(msg("TP1 hit ✅ move SL to BE", 503, reply_to=501)))
    (pos,) = fake.positions.values()
    assert pos.sl == 0.69402 and pos.tp == 0.69563
    run(p.handle_message(msg(AUDUSD_CLOSE, 504, reply_to=501)))
    assert fake.positions == {}


def test_rted_mode_edit_keeps_tp2():
    fake = FakeMt5()
    p, _ = make(fake, tp_mode="last")
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    run(p.handle_message(msg(AUDUSD_SIGNAL.replace("0.69563", "0.69600"), 501, edited=True)))
    (pos,) = fake.positions.values()
    assert pos.tp == 0.69600


def test_entry_after_adverse_move_is_allowed_with_same_risk():
    """Manual: entering after price moved against the signal (but not past SL) is fine."""
    fake = FakeMt5()
    fake.ticks["AUDUSD"] = (0.69350, 0.69352)  # 4.9 pips below the signal's 0.69401, SL at 0.69311
    p, _ = make(fake, tp_mode="last")
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    (send,) = fake.market_sends()
    # 50 USD / (0.00041 * 100000) = 1.21 lots: tighter SL, bigger size, same 0.5% risk
    assert send["volume"] == 1.21


def test_entry_after_move_towards_tp_is_skipped():
    fake = FakeMt5()
    fake.ticks["AUDUSD"] = (0.69430, 0.69432)  # 3.1 pips towards TP > 0.3 x 9 pips
    p, _ = make(fake, tp_mode="last")
    run(p.handle_message(msg(AUDUSD_SIGNAL, 501)))
    assert fake.market_sends() == []
    assert p.storage.get_signal("-1001:501:0")["status"] == "REJECTED"


def test_connect_without_login_attaches_to_terminal_and_still_checks_demo():
    fake = FakeMt5()
    settings_creds = Mt5Credentials(expect_demo=True)
    client = Mt5Client(settings_creds, Mt5Cfg(), mt5_module=fake)
    assert run(client.connect())["demo"] is True
    assert "login" not in fake.init_kw
    client = Mt5Client(Mt5Credentials(expect_demo=True), Mt5Cfg(), mt5_module=FakeMt5(demo=False))
    with pytest.raises(ConfigError, match="real"):
        run(client.connect())
