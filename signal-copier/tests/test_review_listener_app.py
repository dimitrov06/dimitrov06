"""Final review of today's changes: the listener, the restart loop and the monitor (fake Telethon, fake MT5).

- a lost Telegram connection ends `run` with an error, so the owner is told and the bot restarts
  (telegram-disconnect-deaf);
- an edit of a live signal post made while the bot was down is handled at start (downtime-edits-lost);
- live messages that arrive during the start-up catch-up wait for it (catchup-reorder-and-dropped-edit);
- an MT5 terminal that stops answering is reported, and a long outage restarts the bot (mt5-down-silent);
- a custom rules file the bot refuses is reported on Telegram and by `check` (P7-old-rules-file-silent-exit).
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

import signal_copier.app as app
from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import XAUUSD_MARKET, XAUUSD_SELL_LIMIT, make
from signal_copier.__main__ import main
from signal_copier.listener.telegram import TelegramListener
from signal_copier.settings import ConfigError, Secrets, Settings
from test_resilience import CHAT, TITLE, FakeTg, entity, fake_telethon, session, tg  # noqa: F401 (fixture)


# ------------------------------------------------- telegram-disconnect-deaf

class DroppingTg(FakeTg):
    """Like Telethon after 'Automatic reconnection failed': run_until_disconnected raises."""

    async def run_until_disconnected(self):
        self.running.set()
        await asyncio.sleep(0.05)
        raise ConnectionError("Automatic reconnection failed 5 time(s)")


def test_lost_telegram_connection_ends_the_listener_with_an_error():
    p = make(StrictMt5(demo=False))
    lis = TelegramListener(DroppingTg([(entity(), TITLE)], {CHAT: []}), [CHAT], p.storage, p.handle_message,
                           notify=p.notifier.send)
    with pytest.raises(ConnectionError):
        asyncio.run(asyncio.wait_for(lis.run(asyncio.Event()), 2))


def test_lost_telegram_connection_stops_run_so_it_restarts_and_the_owner_is_told(monkeypatch):
    p = make(StrictMt5(demo=False))
    p.settings.telegram.channels = [CHAT]
    p.settings.executor.monitor_interval_s = 0.05
    monkeypatch.setattr(app, "build_pipeline", lambda s, sec: (p, p.client, p.notifier))
    monkeypatch.setattr(app, "make_client", lambda *a: DroppingTg([(entity(), TITLE)], {CHAT: []}))
    with pytest.raises(ConnectionError):  # _run_forever restarts on it
        asyncio.run(asyncio.wait_for(app.run(p.settings, NS(tg_api_id=1, tg_api_hash="h")), 3))
    assert any("listener спря с грешка" in t for t in p.notifier.sent)


# ------------------------------------------------- downtime-edits-lost

def test_post_edited_to_cancelled_while_down_cancels_the_pending_order_at_start():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.10, 4150.40)
    p = make(fake)
    p.test_clock["t"] = NOW + timedelta(seconds=2)
    history = {CHAT: []}
    asyncio.run(session(p, history, [tg(1522, XAUUSD_SELL_LIMIT)]))
    assert len(fake.orders) == 1
    m = history[CHAT][0]  # bot down: RTED edits its own post
    m.raw_text = XAUUSD_SELL_LIMIT + "\n\nCANCELLED ❌"
    history[CHAT].append(tg(1523, "Good morning traders", date=NOW + timedelta(minutes=21)))
    p.test_clock["t"] = NOW + timedelta(minutes=22)
    asyncio.run(session(p, history, now=NOW + timedelta(minutes=22)))
    assert fake.orders == {}
    assert any("отменена чакаща поръчка" in t for t in p.notifier.sent)


def test_tighter_sl_edited_while_down_is_applied_at_start_without_new_messages():
    fake = StrictMt5(demo=False)
    p = make(fake)
    p.test_clock["t"] = NOW + timedelta(seconds=2)
    history = {CHAT: []}
    asyncio.run(session(p, history, [tg(1520, XAUUSD_MARKET)]))
    (pos,) = fake.positions.values()
    history[CHAT][0].raw_text = XAUUSD_MARKET.replace("Stop Loss @ 4470.38", "Stop Loss @ 4474.00")
    p.test_clock["t"] = NOW + timedelta(minutes=6)
    asyncio.run(session(p, history, now=NOW + timedelta(minutes=6)))
    assert pos.sl == 4474.00


def test_unchanged_live_post_is_not_handled_again_at_start():
    fake = StrictMt5(demo=False)
    p = make(fake)
    p.test_clock["t"] = NOW + timedelta(seconds=2)
    history = {CHAT: []}
    asyncio.run(session(p, history, [tg(1520, XAUUSD_MARKET)]))
    n = len(p.notifier.sent)
    p.test_clock["t"] = NOW + timedelta(minutes=6)
    asyncio.run(session(p, history, now=NOW + timedelta(minutes=6)))
    assert len(fake.market_sends()) == 1
    assert [t for t in p.notifier.sent[n:] if not t.startswith("👂")] == []


# ------------------------------------------------- catchup-reorder-and-dropped-edit

def _first_session(p, history, at):
    async def s1():
        client = FakeTg([(entity(), TITLE)], history)
        lis = TelegramListener(client, [CHAT], p.storage, p.handle_message, notify=p.notifier.send, clock=lambda: at)
        stop = asyncio.Event()
        task = asyncio.create_task(lis.run(stop))
        await client.running.wait()
        await client.post(tg(1521, "Gold looking good today", date=at))
        stop.set()
        await task
    asyncio.run(s1())


def _restart_with_live_event(p, history, now, live):
    """Restart; while the catch-up handles 1522, `live(client)` happens on the channel."""
    real = p._handle_message
    holder = {}

    async def slow(msg):
        if msg.message_id == 1522 and "t" not in holder:
            holder["t"] = asyncio.create_task(live(holder["client"]))
            await asyncio.sleep(0.01)
        await real(msg)
    p._handle_message = slow

    async def s2():
        client = FakeTg([(entity(), TITLE)], history)
        holder["client"] = client
        lis = TelegramListener(client, [CHAT], p.storage, p.handle_message, notify=p.notifier.send, clock=lambda: now)
        stop = asyncio.Event()
        task = asyncio.create_task(lis.run(stop))
        await client.running.wait()
        await holder["t"]
        for _ in range(5):
            await asyncio.sleep(0)
        stop.set()
        await task
    asyncio.run(s2())


def _setup_downtime(p):
    history = {CHAT: [tg(1520, "Good morning", date=NOW - timedelta(minutes=5))]}
    p.test_clock["t"] = NOW
    _first_session(p, history, NOW)
    history[CHAT] += [tg(1522, "Get ready", date=NOW + timedelta(seconds=5)),
                      tg(1523, XAUUSD_MARKET, date=NOW + timedelta(seconds=8))]
    now = NOW + timedelta(seconds=20)
    p.test_clock["t"] = now
    return history, now


def test_live_close_during_the_catch_up_waits_for_the_signal_it_replies_to():
    fake = StrictMt5(demo=False)
    p = make(fake)
    history, now = _setup_downtime(p)

    async def close(client):
        await client.post(tg(1524, "Close XAUUSD now", date=now, reply_to=1523))
    _restart_with_live_event(p, history, now, close)
    assert fake.positions == {}  # the caught-up signal was opened, then closed by the live command
    assert ("REJECTED", "target_signal_not_found") not in [(e["decision"], e["reason"])
                                                           for e in p.storage.events("follow_up")]


def test_live_edit_of_a_caught_up_signal_is_not_lost():
    fake = StrictMt5(demo=False)
    p = make(fake)
    history, now = _setup_downtime(p)

    async def fix_sl(client):
        # a new object, as Telethon delivers it: the catch-up still holds the version it fetched before the edit
        old = next(x for x in history[CHAT] if x.id == 1523)
        m = tg(1523, XAUUSD_MARKET.replace("Stop Loss @ 4470.38", "Stop Loss @ 4474.00"), date=old.date)
        history[CHAT][history[CHAT].index(old)] = m
        for ev, fn in client.handlers:
            if type(ev).__name__ == "MessageEdited":
                await fn(NS(chat_id=CHAT, message=m))
    _restart_with_live_event(p, history, now, fix_sl)
    (pos,) = fake.positions.values()
    assert pos.sl == 4474.00


# ------------------------------------------------- mt5-down-silent

def test_mt5_that_stops_answering_is_reported_once_and_restarts_the_bot():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post_text = len(p.notifier.sent)
    fake.ipc_down = True  # terminal closed / crashed / updating
    with pytest.raises(RuntimeError, match="rounds in a row"):  # _run_forever restarts: initialize() again
        asyncio.run(asyncio.wait_for(app._monitor(p, 0, asyncio.Event()), 5))
    alerts = [t for t in p.notifier.sent[post_text:] if t.startswith("❗ Брокерът не отговаря")]
    assert len(alerts) == 1


def test_mt5_that_answers_again_is_reported_and_the_monitor_goes_on():
    fake = StrictMt5(demo=False)
    p = make(fake)
    fake.ipc_down = True
    real = p.monitor_once
    rounds = {"n": 0}
    stop = asyncio.Event()

    async def once():
        rounds["n"] += 1
        if rounds["n"] == app.MONITOR_ALERT_AFTER + 2:
            fake.ipc_down = False
        if rounds["n"] == app.MONITOR_ALERT_AFTER + 3:
            stop.set()
        await real()
    p.monitor_once = once
    asyncio.run(asyncio.wait_for(app._monitor(p, 0, stop), 5))
    assert any(t.startswith("❗ Брокерът не отговаря") for t in p.notifier.sent)
    assert p.notifier.sent[-1] == "✅ Брокерът отговаря отново."


def test_short_mt5_hiccup_sends_nothing():
    fake = StrictMt5(demo=False)
    p = make(fake)
    fake.fail = {"account_info": 1}
    stop = asyncio.Event()
    real = p.monitor_once
    rounds = {"n": 0}

    async def once():
        rounds["n"] += 1
        if rounds["n"] == 3:
            stop.set()
        await real()
    p.monitor_once = once
    asyncio.run(asyncio.wait_for(app._monitor(p, 0, stop), 5))
    assert p.notifier.sent == []


# ------------------------------------------------- P7-old-rules-file-silent-exit

@pytest.fixture
def old_rules(tmp_path):
    path = tmp_path / "my_rules.yaml"
    path.write_text("symbols: {GOLD: XAU_USD}\nfx_currencies: [USD]\n", encoding="utf-8")  # yesterday's layout
    return path


def test_check_refuses_a_rules_file_that_run_would_refuse(tmp_path, old_rules, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"mode: record\nparser:\n  rules_file: '{old_rules}'\n", encoding="utf-8")
    assert main(["check", "--config", str(cfg), "--env", str(tmp_path / "none.env")]) == 2
    assert "lack" in capsys.readouterr().err


def test_run_with_a_refused_rules_file_tells_the_owner(tmp_path, old_rules, monkeypatch):
    sent = []

    class RecordingNotifier:
        def __init__(self, token, chat_id):
            pass

        async def send(self, text):
            sent.append(text)

        async def aclose(self):
            pass

    monkeypatch.setattr(app, "Notifier", RecordingNotifier)
    settings = Settings.model_validate({"mode": "record", "telegram": {"channels": [CHAT]},
                                        "parser": {"rules_file": str(old_rules)},
                                        "storage": {"sqlite_path": str(tmp_path / "bot.db")}})
    with pytest.raises(ConfigError):
        asyncio.run(app.run(settings, Secrets(tg_api_id=1, tg_api_hash="h")))
    assert len(sent) == 1 and sent[0].startswith("❗ signal-copier не стартира") and "lack" in sent[0]
