"""MT5 failure modes on a real-money account: lost answers, None reads, fills between monitor rounds, and a
terminal that cannot trade. Owner config (live, 3% risk, tp_split last) and real RTED samples throughout.
"""

import asyncio
import sys
from datetime import timedelta

import pytest

from conftest import NOW
from fake_mt5 import FakeMt5
from signal_copier.executor.mt5 import Mt5Client, Mt5Error, Mt5Executor
from signal_copier.models import IncomingMessage
from signal_copier.notifier import Notifier
from signal_copier.parser import SignalParser
from signal_copier.pipeline import Pipeline
from signal_copier.risk import RiskManager
from signal_copier.settings import ConfigError, Mt5Credentials, Secrets, Settings
from signal_copier.storage import Storage

AUDUSD_SIGNAL = """RTED Investing - Premium Trading Signals 🏆
AUDUSD
Buy now @ 0.69401
Target Profit 1 @ 0.69446
Target Profit 2 @ 0.69563
Stop Loss @ 0.69311"""
AUDUSD_CLOSE = "‼️ Close AUDUSD Manually now at 0.69530! (+1.43%)"
XAUUSD_MARKET = """RTED Investing - Premium Trading Signals 🏆
XAUUSD
Buy Market Order @ 4479.69
Target Profit 1 @ 4484.30
Target Profit 2 @ 4498.30
Stop Loss @ 4470.38"""
XAUUSD_CLOSE = "‼️ Close XAUUSD Manually now at 4485.10! (+0.50%)"
XAUUSD_SELL_LIMIT = ("RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nSell Limit Order @ 4156.56\n"
                     "Target Profit 1 @ 4153.58\nTarget Profit 2 @ 4142.80\nStop Loss @ 4162.55")
XAUUSD_CANCEL = "Cancel Pending Order on XAUUSD"
NOTHING_OPEN = "няма отворени сделки/поръчки"


def run(coro):
    return asyncio.run(coro)


def msg(text, message_id, reply_to=None, edited=False):
    return IncomingMessage(chat_id=-1001, message_id=message_id, text=text, date=NOW, reply_to_msg_id=reply_to,
                           edited=edited)


def owner_settings() -> Settings:
    return Settings.model_validate({
        "mode": "live",
        "risk": {"risk_per_trade_pct": "3", "daily_loss_limit_pct": "6", "max_open_positions": 3},
        "executor": {"tp_split": "last", "retry_attempts": 4, "retry_base_delay_s": 1.0},
    })


def make(fake: FakeMt5, on_sleep=None) -> Pipeline:
    settings = owner_settings()
    client = Mt5Client(Mt5Credentials(expect_demo=False), settings.mt5, mt5_module=fake)
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        if on_sleep is not None:
            on_sleep(len(slept))

    client.sleep = sleep
    storage = Storage(":memory:")
    ex = settings.executor
    executor = Mt5Executor(client, storage, ex.retry_attempts, ex.retry_base_delay_s,
                           ex.pending_order_expiry_minutes, ex.tp_split)
    p = Pipeline(settings, storage, SignalParser(), Notifier(None, None), RiskManager(settings.risk), client,
                 executor, clock=lambda: NOW + timedelta(seconds=5))
    p.slept = slept
    return p


def then_ipc_down(fake: FakeMt5) -> None:
    """The next order_send executes, then the terminal's IPC drops (answer lost)."""
    orig = fake.order_send

    def send(req):
        orig(req)
        fake.order_send = orig
        fake.ipc_down = True
        return None

    fake.order_send = send


def statuses(p, key):
    return [o["status"] for o in p.storage.orders_for_signal(key)]


# ------------------------------------------------------------- C1: lost answer -> never a second position

def test_lost_answer_then_one_none_from_each_read_is_not_resent():
    fake = FakeMt5(demo=False)
    fake.script = ["lost"]
    fake.fail = {"positions_get": 1, "orders_get": 1, "history_deals_get": 1}
    p = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 601)))
    assert len(fake.market_sends()) == 1 and len(fake.positions) == 1
    assert [t["trade_id"] for t in p.storage.trades_for_signal("-1001:601:0")] == [str(next(iter(fake.positions)))]
    assert p.storage.get_signal("-1001:601:0")["status"] == "EXECUTED"


def test_lost_answer_then_short_ipc_outage_is_not_resent():
    fake = FakeMt5(demo=False)
    then_ipc_down(fake)
    p = make(fake, on_sleep=lambda n: setattr(fake, "ipc_down", False))  # back after the first backoff
    run(p.handle_message(msg(AUDUSD_SIGNAL, 602)))
    assert len(fake.market_sends()) == 1 and len(fake.positions) == 1
    assert statuses(p, "-1001:602:0") == ["FILLED"]


def test_lost_answer_long_outage_stays_unknown_then_adopted_and_closable():
    fake = FakeMt5(demo=False)
    then_ipc_down(fake)
    p = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 603)))
    assert len(fake.market_sends()) == 1 and len(fake.positions) == 1
    assert sum(p.slept) == 15  # looked for it for 1+2+4+8 s, never sent again
    assert statuses(p, "-1001:603:0") == ["SENT"]
    sig = p.storage.get_signal("-1001:603:0")
    assert sig["status"] == "EXECUTED" and "НЕ я пращам повторно" in sig["reason"]
    assert any(t.startswith("❗") and "провери терминала" in t for t in p.notifier.sent)
    assert len(p.storage.open_positions()) == 1  # the slot stays taken while unknown

    fake.ipc_down = False
    run(p.monitor_once())
    assert statuses(p, "-1001:603:0") == ["FILLED"]
    assert any("Поръчката без отговор е изпълнена" in t for t in p.notifier.sent)
    run(p.handle_message(msg(AUDUSD_CLOSE, 604, reply_to=603)))
    assert fake.positions == {}


@pytest.mark.parametrize("mode", ["timeout_late", "none_late"])
def test_late_fill_after_ambiguous_answer_is_found_not_resent(mode):
    fake = FakeMt5(demo=False)
    fake.script = [mode]
    p = make(fake, on_sleep=lambda n: fake.settle())  # the server fills it during the first backoff
    run(p.handle_message(msg(AUDUSD_SIGNAL, 701)))
    assert len(fake.market_sends()) == 1 and len(fake.positions) == 1
    tracked = {t["trade_id"] for t in p.storage.trades_for_signal("-1001:701:0")}
    assert tracked == {str(t) for t in fake.positions}
    run(p.handle_message(msg(AUDUSD_CLOSE, 702, reply_to=701)))
    assert fake.positions == {}


def test_pending_order_with_lost_answer_is_never_failed_and_is_adopted():
    fake = FakeMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)  # the sell limit is above the market
    then_ipc_down(fake)
    p = make(fake)
    run(p.handle_message(msg(XAUUSD_SELL_LIMIT, 950)))
    assert len(fake.orders) == 1 and len(fake.sent) == 1
    assert statuses(p, "-1001:950:0") == ["SENT"]  # not FAILED: it lives at the broker
    assert not any("not sent" in t for t in p.notifier.sent)

    fake.ipc_down = False
    run(p.monitor_once())  # found among the working orders by its tag
    ticket = next(iter(fake.orders))
    assert [(o["status"], o["oanda_order_id"]) for o in p.storage.orders_for_signal("-1001:950:0")] == [
        ("PENDING", str(ticket))]
    assert any("Поръчката без отговор е чакаща" in t for t in p.notifier.sent)
    fake.fill_pending(ticket)
    run(p.monitor_once())
    run(p.handle_message(msg(XAUUSD_CLOSE, 951, reply_to=950)))
    assert fake.positions == {}


@pytest.mark.parametrize("offset_h", [2, 3])
def test_fresh_deal_is_found_despite_server_time(offset_h):
    """Lost answer and the position is already stopped out: only the IN deal (server-time stamp) shows it."""
    fake = FakeMt5(demo=False, server_offset=offset_h * 3600)
    orig = fake.order_send

    def send_then_stopped_out(req):
        res = orig(req)
        for t in list(fake.positions):
            fake.hit_tp(t, 0.69311)
        return res

    fake.order_send = send_then_stopped_out
    fake.script = ["lost"]
    p = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 801)))
    assert len(fake.market_sends()) == 1
    assert statuses(p, "-1001:801:0") == ["FILLED"]
    run(p.monitor_once())  # the trade row lets sync report the close and its P/L
    assert p.storage.open_trades() == []
    assert any(t.startswith("🏁 Затворена") for t in p.notifier.sent)


def test_tag_only_in_deal_but_position_open_is_tracked():
    """The broker changed the position comment: the IN deal has the tag, the position is still open."""
    fake = FakeMt5(demo=False)
    orig = fake.order_send

    def send_and_rename(req):
        res = orig(req)
        for pos in fake.positions.values():
            pos.comment = ""
        return res

    fake.order_send = send_and_rename
    fake.script = ["lost"]
    p = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 901)))
    assert len(fake.market_sends()) == 1
    assert [t["state"] for t in p.storage.trades_for_signal("-1001:901:0")] == ["OPEN"]
    assert len(p.storage.open_positions()) == 1
    run(p.handle_message(msg(AUDUSD_CLOSE, 902, reply_to=901)))
    assert fake.positions == {}


# ------------------------------------------------------------- C2: a None read is never "nothing open"

def _open_audusd(mid=501):
    fake = FakeMt5(demo=False)
    p = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, mid)))
    (pos,) = fake.positions.values()
    assert pos.volume == 3.29
    return fake, p


def test_close_during_ipc_outage_alerts_and_an_edit_retries_it():
    fake, p = _open_audusd()
    fake.ipc_down = True
    run(p.handle_message(msg(AUDUSD_CLOSE, 502, reply_to=501)))
    assert p.storage.get_follow_up("-1001:502:CLOSE:AUD_USD")["status"] == "FAILED"
    assert p.notifier.sent[-1].startswith("❗") and NOTHING_OPEN not in p.notifier.sent[-1]
    fake.ipc_down = False
    run(p.handle_message(msg(AUDUSD_CLOSE + " ✅", 502, reply_to=501, edited=True)))
    assert fake.positions == {}
    assert p.storage.get_follow_up("-1001:502:CLOSE:AUD_USD")["status"] == "EXECUTED"
    run(p.handle_message(msg(AUDUSD_CLOSE + " ✅✅", 502, reply_to=501, edited=True)))  # done: never again
    assert len([r for r in fake.sent if "position" in r]) == 1


@pytest.mark.parametrize("text,key", [("Close now", "CLOSE"), ("Move SL to BE", "MOVE_SL_BE"),
                                      ("Close half", "CLOSE_PARTIAL")])
def test_single_failed_positions_get_is_an_error_not_nothing_to_do(text, key):
    fake, p = _open_audusd()
    fake.fail = {"positions_get": 1}
    run(p.handle_message(msg(text, 503, reply_to=501)))
    fu = p.storage.get_follow_up(f"-1001:503:{key}")
    assert fu["status"] == "FAILED" and "positions_get failed" in fu["reason"]
    assert not any(NOTHING_OPEN in t for t in p.notifier.sent)
    (pos,) = fake.positions.values()
    assert pos.volume == 3.29 and pos.sl == 0.69311


def test_close_partial_is_not_retried_on_edit():
    """Closing half twice would close 75%: a FAILED partial close stays failed."""
    fake, p = _open_audusd()
    fake.fail = {"positions_get": 1}
    run(p.handle_message(msg("Close half", 503, reply_to=501)))
    run(p.handle_message(msg("Close half now", 503, reply_to=501, edited=True)))
    assert fake.positions[next(iter(fake.positions))].volume == 3.29


def test_signal_edit_with_failed_read_keeps_old_levels_so_it_can_be_retried():
    fake, p = _open_audusd()
    fake.fail = {"positions_get": 1}
    edited = AUDUSD_SIGNAL.replace("Stop Loss @ 0.69311", "Stop Loss @ 0.69350")
    run(p.handle_message(msg(edited, 501, edited=True)))
    assert p.notifier.sent[-1].startswith("❗")
    assert p.storage.get_signal("-1001:501:0")["sl"] == "0.69311"  # DB matches MT5
    run(p.handle_message(msg(edited + "\n(updated)", 501, edited=True)))
    (pos,) = fake.positions.values()
    assert pos.sl == 0.6935 and p.storage.get_signal("-1001:501:0")["sl"] == "0.69350"


def _xau_half_closed():
    fake = FakeMt5(demo=False)
    p = make(fake)
    run(p.handle_message(msg(XAUUSD_MARKET, 40)))
    run(p.handle_message(msg("Close half", 41, reply_to=40)))
    (pos,) = fake.positions.values()
    assert pos.volume == 0.16
    return fake, p


def test_sync_with_failed_positions_get_does_not_close_half_closed_trade():
    fake, p = _xau_half_closed()
    fake.fail = {"positions_get": 1}
    run(p.monitor_once())
    assert len(p.storage.open_trades()) == 1
    assert not any(t.startswith("🏁") for t in p.notifier.sent)
    run(p.handle_message(msg(XAUUSD_CLOSE, 42, reply_to=40)))
    assert fake.positions == {}
    run(p.monitor_once())
    assert p.storage.open_trades() == [] and any(t.startswith("🏁") for t in p.notifier.sent)


def test_sync_with_empty_answer_does_not_close_half_closed_trade():
    """Only OUT volume >= IN volume is a close; a partial close in the history is not."""
    fake, p = _xau_half_closed()
    orig = fake.positions_get
    fake.positions_get = lambda symbol=None, ticket=None: ()
    run(p.monitor_once())
    fake.positions_get = orig
    assert len(p.storage.open_trades()) == 1


# ------------------------------------------------------------- C5: follow-ups right after a pending fill

def _sell_limit(mid=1522):
    fake = FakeMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    run(p.handle_message(msg(XAUUSD_SELL_LIMIT, mid)))
    (ticket,) = fake.orders
    return fake, p, ticket


def _fill_on_remove(fake):
    """The limit fills just as the bot asks MT5 to remove it."""
    orig = fake.order_send

    def send(req):
        if req["action"] == fake.TRADE_ACTION_REMOVE and req["order"] in fake.orders:
            fake.fill_pending(req["order"])
        return orig(req)

    fake.order_send = send


@pytest.mark.parametrize("race", [False, True])
def test_close_after_unsynced_fill_closes_the_position(race):
    fake, p, ticket = _sell_limit()
    if race:
        _fill_on_remove(fake)
    else:
        fake.fill_pending(ticket)
    run(p.handle_message(msg(XAUUSD_CLOSE, 1523, reply_to=1522)))
    assert fake.positions == {} and fake.orders == {}
    assert statuses(p, "-1001:1522:0") == ["FILLED"]
    assert f"затворена #{ticket}" in p.notifier.sent[-1]
    run(p.monitor_once())
    assert p.storage.open_trades() == [] and any(t.startswith("🏁") for t in p.notifier.sent)


@pytest.mark.parametrize("race", [False, True])
def test_cancel_after_unsynced_fill_adopts_and_says_it_is_open(race):
    fake, p, ticket = _sell_limit()
    if race:
        _fill_on_remove(fake)
    else:
        fake.fill_pending(ticket)
    run(p.handle_message(msg(XAUUSD_CANCEL, 1523, reply_to=1522)))
    assert list(fake.positions) == [ticket]  # a cancel never closes a position
    assert statuses(p, "-1001:1522:0") == ["FILLED"]
    assert [t["trade_id"] for t in p.storage.open_trades()] == [str(ticket)]
    assert len(p.storage.open_positions()) == 1
    last = p.notifier.sent[-1]
    assert f"позиция #{ticket}" in last and "остава отворена" in last and NOTHING_OPEN not in last


def test_cancel_after_synced_fill_says_it_is_open():
    fake, p, ticket = _sell_limit()
    fake.fill_pending(ticket)
    run(p.monitor_once())
    run(p.handle_message(msg(XAUUSD_CANCEL, 1523, reply_to=1522)))
    assert list(fake.positions) == [ticket]
    assert "остава отворена" in p.notifier.sent[-1] and NOTHING_OPEN not in p.notifier.sent[-1]


def test_cancel_during_ipc_outage_keeps_order_pending_and_alerts():
    fake, p, ticket = _sell_limit()
    fake.ipc_down = True
    run(p.handle_message(msg(XAUUSD_CANCEL, 1523, reply_to=1522)))
    fake.ipc_down = False
    assert statuses(p, "-1001:1522:0") == ["PENDING"] and ticket in fake.orders
    assert p.storage.get_follow_up("-1001:1523:CANCEL:XAU_USD")["status"] == "FAILED"
    assert p.notifier.sent[-1].startswith("❗")
    fake.fill_pending(ticket)
    run(p.monitor_once())  # still tracked: adopted when it fills
    assert [t["trade_id"] for t in p.storage.open_trades()] == [str(ticket)]


def test_cancel_whose_remove_answer_is_lost_checks_history():
    fake, p, ticket = _sell_limit()
    fake.script = ["lost"]  # REMOVE executes, the answer is lost
    run(p.handle_message(msg(XAUUSD_CANCEL, 1523, reply_to=1522)))
    assert fake.orders == {} and statuses(p, "-1001:1522:0") == ["CANCELLED"]
    assert p.storage.get_follow_up("-1001:1523:CANCEL:XAU_USD")["status"] == "EXECUTED"


def test_cancel_whose_remove_never_arrived_is_an_error():
    fake, p, ticket = _sell_limit()
    fake.script = ["none"]  # REMOVE never reached the server
    run(p.handle_message(msg(XAUUSD_CANCEL, 1523, reply_to=1522)))
    assert ticket in fake.orders and statuses(p, "-1001:1522:0") == ["PENDING"]
    assert p.storage.get_follow_up("-1001:1523:CANCEL:XAU_USD")["status"] == "FAILED"


def test_move_be_between_fill_and_sync_is_applied():
    fake, p, ticket = _sell_limit()
    fake.fill_pending(ticket)
    run(p.handle_message(msg("TP1 hit ✅ move SL to BE", 1524, reply_to=1522)))
    assert fake.positions[ticket].sl == 4156.56
    run(p.monitor_once())  # already adopted: no second "filled" notice, trade tracked once
    assert len(p.storage.open_trades()) == 1


def test_close_partial_below_minimum_says_so():
    fake = FakeMt5(demo=False)
    fake.balance = fake.equity = 200.0  # 3% of 200 over a 5.99 USD stop -> 0.01 lot of gold
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    run(p.handle_message(msg(XAUUSD_SELL_LIMIT, 40)))
    (ticket,) = fake.orders
    fake.fill_pending(ticket)
    run(p.handle_message(msg("Close half", 41, reply_to=40)))
    assert fake.positions[ticket].volume == 0.01
    assert "твърде малко" in p.notifier.sent[-1] and NOTHING_OPEN not in p.notifier.sent[-1]


# ------------------------------------------------------------- C6: unknown sends stay manageable

def test_symbol_only_close_with_an_unknown_second_signal_is_ambiguous():
    fake = FakeMt5(demo=False)
    p = make(fake)
    run(p.handle_message(msg(XAUUSD_MARKET, 590)))
    then_ipc_down(fake)
    run(p.handle_message(msg(XAUUSD_MARKET.replace("4479.69", "4479.70"), 601)))
    fake.ipc_down = False
    run(p.monitor_once())
    assert len(fake.positions) == 2
    run(p.handle_message(msg("Close XAUUSD now", 610)))
    fu = p.storage.get_follow_up("-1001:610:CLOSE:XAU_USD")
    assert (fu["status"], fu["reason"]) == ("REJECTED", "ambiguous_target") and len(fake.positions) == 2


def test_sync_revives_a_failed_signal_whose_order_turns_up():
    fake = FakeMt5(demo=False)
    then_ipc_down(fake)
    p = make(fake)
    run(p.handle_message(msg(AUDUSD_SIGNAL, 620)))
    p.storage.set_signal_status("-1001:620:0", "FAILED")  # as older versions stored it
    fake.ipc_down = False
    run(p.monitor_once())
    assert p.storage.get_signal("-1001:620:0")["status"] == "EXECUTED"
    fake.ticks["AUDUSD"] = (0.69450, 0.69452)  # TP1 0.69446 reached
    run(p.handle_message(msg("TP1 hit ✅ move SL to BE", 621, reply_to=620)))
    (pos,) = fake.positions.values()
    assert pos.sl == pos.price_open


def test_cancel_reaches_a_pending_order_still_in_unknown_state():
    """The sell limit's answer was lost and no monitor round has run yet: cancel still finds and removes it."""
    fake = FakeMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    then_ipc_down(fake)
    p = make(fake)
    run(p.handle_message(msg(XAUUSD_SELL_LIMIT, 1522)))
    fake.ipc_down = False
    run(p.handle_message(msg(XAUUSD_CANCEL, 1523, reply_to=1522)))
    assert fake.orders == {} and statuses(p, "-1001:1522:0") == ["CANCELLED"]


# ------------------------------------------------------------- C21: a terminal/account that cannot trade

@pytest.mark.parametrize("attr,value,reason", [
    ("algo_trading", False, "Algo Trading"),
    ("tradeapi_disabled", True, "Python API"),
    ("account_trade_allowed", False, "investor"),
    ("trade_expert", False, "брокерът"),
    ("margin_mode", FakeMt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING, "hedging"),
])
def test_connect_refuses_a_terminal_or_account_that_cannot_trade(attr, value, reason):
    fake = FakeMt5(demo=False)
    setattr(fake, attr, value)
    client = Mt5Client(Mt5Credentials(expect_demo=False), owner_settings().mt5, mt5_module=fake)
    with pytest.raises(ConfigError, match=reason):
        run(client.connect())


@pytest.mark.parametrize("setup", ["not_connected", "no_answer"])
def test_connect_to_a_terminal_still_starting_is_refused_but_retryable(setup):
    """At boot the terminal answers before it has reached the broker: refused, but not a fatal config error,
    so the restart loop in __main__ tries again (C19 fatal-error-exits-process-no-restart-on-windows)."""
    fake = FakeMt5(demo=False)
    if setup == "not_connected":
        fake.connected = False
    else:
        fake.fail = {"terminal_info": 1}
    client = Mt5Client(Mt5Credentials(expect_demo=False), owner_settings().mt5, mt5_module=fake)
    with pytest.raises(Mt5Error, match="още не може да търгува") as e:
        run(client.connect())
    assert not isinstance(e.value, ConfigError)
    assert fake.sent == []


def test_check_reports_blocked_trading(monkeypatch, capsys):
    from signal_copier.__main__ import _check

    fake = FakeMt5(demo=False)
    fake.algo_trading = False
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    record = Settings.model_validate({"mode": "record", "broker": "mt5"})
    run(_check(record, Secrets()))
    assert "trading BLOCKED: бутонът Algo Trading" in capsys.readouterr().out
    live = Settings.model_validate({"mode": "live", "broker": "mt5"})
    with pytest.raises(ConfigError, match="Algo Trading"):
        run(_check(live, Secrets(live_confirm="yes")))


def test_monitor_notifies_once_when_trading_gets_blocked_and_again_when_back():
    fake = FakeMt5(demo=False)
    p = make(fake)
    run(p.monitor_once())
    fake.algo_trading = False
    run(p.monitor_once())
    run(p.monitor_once())
    blocked = [t for t in p.notifier.sent if t.startswith("🚫 MT5")]
    assert len(blocked) == 1 and "Algo Trading" in blocked[0]
    fake.algo_trading = True
    run(p.monitor_once())
    assert p.notifier.sent[-1] == "✅ MT5 отново може да търгува."


def test_run_refuses_to_start_live_with_algo_trading_off_and_says_so(monkeypatch):
    import signal_copier.app as app

    fake = FakeMt5(demo=False)
    fake.algo_trading = False
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    captured = {}
    orig_build = app.build_pipeline

    def build(settings, secrets):
        p, c, n = orig_build(settings, secrets)
        captured["notifier"] = n
        return p, c, n

    monkeypatch.setattr(app, "build_pipeline", build)
    settings = Settings.model_validate({"mode": "live", "broker": "mt5", "telegram": {"channels": ["RTED"]},
                                        "storage": {"sqlite_path": ":memory:"}})
    with pytest.raises(ConfigError, match="Algo Trading"):
        run(app.run(settings, Secrets(tg_api_id=1, tg_api_hash="x", live_confirm="yes")))
    sent = captured["notifier"].sent
    assert sent and sent[-1].startswith("❗ signal-copier не стартира")
    assert not any("стартира в режим" in t for t in sent)
    assert fake.sent == []
