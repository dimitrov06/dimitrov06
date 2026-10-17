"""Restarts and outages: messages posted while the bot was down, a renamed group, a crash at boot, a hanging Bot API.

Telethon is not installed in the test environment: a minimal fake `telethon` module and client stand in. Like real
Telethon with catch_up=False, the client delivers only what is posted while it runs; the rest is in its history.
"""

import asyncio
import sys
import types
from datetime import timedelta
from types import SimpleNamespace as NS

import httpx
import pytest

from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import XAUUSD_CANCEL, XAUUSD_CLOSE, XAUUSD_MARKET, XAUUSD_SELL_LIMIT, make
from signal_copier.listener.telegram import TelegramListener, resolve_channel
from signal_copier.notifier import Notifier
from signal_copier.settings import ConfigError
from signal_copier.storage import Storage

CHAT = -1001
TITLE = "RTED Investing - Premium Trading Signals SP 🏆"


@pytest.fixture(autouse=True)
def fake_telethon(monkeypatch):
    tl, ev, ut = (types.ModuleType(n) for n in ("telethon", "telethon.events", "telethon.utils"))
    ev.NewMessage = type("NewMessage", (), {"__init__": lambda self, chats=None: setattr(self, "chats", chats)})
    ev.MessageEdited = type("MessageEdited", (), {"__init__": lambda self, chats=None: setattr(self, "chats", chats)})
    ut.get_peer_id = lambda entity: entity.peer_id
    tl.events, tl.utils = ev, ut
    for name, mod in (("telethon", tl), ("telethon.events", ev), ("telethon.utils", ut)):
        monkeypatch.setitem(sys.modules, name, mod)


def tg(mid, text, date=NOW, reply_to=None):
    return NS(id=mid, raw_text=text, date=date, edit_date=None,
              reply_to=NS(reply_to_msg_id=reply_to) if reply_to else None)


class FakeTg:
    def __init__(self, dialogs, history):
        self.dialogs, self.history, self.handlers = dialogs, history, []
        self.running, self._disc = asyncio.Event(), asyncio.Event()

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_entity(self, x):
        return next(e for e, _ in self.dialogs if e.peer_id == x)

    async def get_dialogs(self):
        return [NS(entity=e, name=t) for e, t in self.dialogs]

    async def get_messages(self, entity, limit=None, min_id=0, reverse=False, ids=None):
        if ids is not None:  # like Telethon: the current version of each, None for a deleted one
            by_id = {m.id: m for m in self.history.get(entity.peer_id, [])}
            return [by_id.get(i) for i in ids]
        msgs = sorted((m for m in self.history.get(entity.peer_id, []) if m.id > (min_id or 0)),
                      key=lambda m: m.id, reverse=not reverse)
        return msgs[:limit] if limit else msgs

    def on(self, ev):
        def deco(fn):
            self.handlers.append((ev, fn))
            return fn
        return deco

    async def run_until_disconnected(self):
        self.running.set()
        await self._disc.wait()

    async def disconnect(self):
        self._disc.set()

    async def post(self, m):
        self.history.setdefault(CHAT, []).append(m)
        for ev, fn in self.handlers:
            if type(ev).__name__ == "NewMessage":
                await fn(NS(chat_id=CHAT, message=m))


def entity(title=TITLE):
    return NS(peer_id=CHAT, title=title)


async def session(p, history, live=(), now=NOW + timedelta(hours=1), channels=(CHAT,), dialogs=None):
    client = FakeTg(dialogs or [(entity(), TITLE)], history)
    stop = asyncio.Event()
    lis = TelegramListener(client, list(channels), p.storage, p.handle_message, notify=p.notifier.send,
                           clock=lambda: now)
    task = asyncio.create_task(lis.run(stop))
    await asyncio.wait_for(client.running.wait(), 2)
    for m in live:
        await client.post(m)
    stop.set()
    await asyncio.wait_for(task, 2)
    return lis


# ------------------------------------------------- messages posted while the bot was down

def test_close_posted_during_downtime_is_applied_at_start():
    fake = StrictMt5(demo=False)
    p = make(fake)
    p.test_clock["t"] = NOW + timedelta(seconds=2)
    history = {CHAT: []}
    asyncio.run(session(p, history, [tg(1520, XAUUSD_MARKET)]))
    assert len(fake.positions) == 1
    history[CHAT].append(tg(1521, XAUUSD_CLOSE, date=NOW + timedelta(minutes=40), reply_to=1520))  # bot is down
    p.test_clock["t"] = NOW + timedelta(hours=1)
    asyncio.run(session(p, history))
    assert fake.positions == {}
    assert any("Докато ботът беше спрян" in t and "1 съобщения" in t for t in p.notifier.sent)


def test_cancel_posted_during_downtime_removes_the_pending_order():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.10, 4150.40)
    p = make(fake)
    p.test_clock["t"] = NOW + timedelta(seconds=2)
    history = {CHAT: []}
    asyncio.run(session(p, history, [tg(1522, XAUUSD_SELL_LIMIT)]))
    assert len(fake.orders) == 1
    history[CHAT] += [tg(1523, XAUUSD_CANCEL, date=NOW + timedelta(minutes=20), reply_to=1522),
                      tg(1524, XAUUSD_MARKET, date=NOW + timedelta(minutes=30))]
    p.test_clock["t"] = NOW + timedelta(hours=1)
    asyncio.run(session(p, history))
    assert fake.orders == {} and fake.positions == {}  # cancelled; the stale signal failed the age check
    assert p.storage.get_signal("-1001:1524:0")["reason"].startswith("signal_too_old")


def test_too_old_gap_is_only_reported():
    fake = StrictMt5(demo=False)
    p = make(fake)
    p.test_clock["t"] = NOW + timedelta(seconds=2)
    history = {CHAT: []}
    asyncio.run(session(p, history, [tg(1520, XAUUSD_MARKET)]))
    history[CHAT].append(tg(1521, XAUUSD_CLOSE, date=NOW + timedelta(minutes=40), reply_to=1520))
    asyncio.run(session(p, history, now=NOW + timedelta(days=3)))
    assert len(fake.positions) == 1
    assert any("НЕ ги обработвам автоматично" in t for t in p.notifier.sent)


def test_first_start_does_not_replay_the_channel():
    fake = StrictMt5(demo=False)
    p = make(fake)
    asyncio.run(session(p, {CHAT: [tg(1500, XAUUSD_CLOSE)]}))
    assert not any("Докато ботът беше спрян" in t for t in p.notifier.sent)
    assert p.storage.last_message_id(CHAT) is None


# ------------------------------------------------- the group is found by its id once matched by name

def test_group_matched_by_name_is_followed_by_id_after_a_rename():
    fake = StrictMt5(demo=False)
    p = make(fake)
    name = "RTED Investing - Premium Trading Signals SP"
    other = NS(peer_id=-1002, title="Trading Signals")
    asyncio.run(session(p, {CHAT: []}, channels=[name], dialogs=[(entity(), TITLE), (other, "Trading Signals")]))
    assert p.storage.get_state("channel_peer:rted investing premium trading signals sp") == str(CHAT)
    renamed = [(entity("RTED VIP Gold Signals"), "RTED VIP Gold Signals"), (other, "Trading Signals")]
    lis = asyncio.run(session(p, {CHAT: []}, channels=[name], dialogs=renamed))
    assert list(lis.watermarks) == [CHAT]
    assert any("вече се казва 'RTED VIP Gold Signals'" in t for t in p.notifier.sent)
    assert any(t.startswith("👂 Следя: RTED VIP Gold Signals (-1001)") for t in p.notifier.sent)


def test_group_left_is_a_config_error_not_another_chat():
    st = Storage(":memory:")
    st.set_state("channel_peer:rted investing", str(CHAT))
    client = FakeTg([(NS(peer_id=-1002, title="RTED Investing"), "RTED Investing")], {})
    with pytest.raises(ConfigError, match="no longer among your Telegram chats"):
        asyncio.run(resolve_channel(client, "RTED Investing", st))


# ------------------------------------------------- a crash restarts the bot, a config error does not

def test_run_restarts_after_a_crash_and_stops_on_config_error(monkeypatch):
    import signal_copier.__main__ as main
    import signal_copier.app as app

    calls, slept = [], []

    async def fake_run(settings, secrets):
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("MT5 initialize failed: (-10005, 'IPC timeout')")
        raise ConfigError("MT5 account is demo, but this mode needs real")

    monkeypatch.setattr(app, "run", fake_run)
    monkeypatch.setattr(main, "_sleep", slept.append)
    monkeypatch.setattr(main, "load", lambda *_: (NS(logging=NS(level="INFO", json_logs=False)), None))
    assert main.main(["run"]) == 2
    assert len(calls) == 3 and slept == [10, 30]


def test_run_returns_normally_when_stopped(monkeypatch):
    import signal_copier.__main__ as main
    import signal_copier.app as app

    async def fake_run(settings, secrets):
        return None

    monkeypatch.setattr(app, "run", fake_run)
    monkeypatch.setattr(main, "_sleep", lambda s: pytest.fail("no restart"))
    monkeypatch.setattr(main, "load", lambda *_: (NS(logging=NS(level="INFO", json_logs=False)), None))
    assert main.main(["run"]) == 0


# ------------------------------------------------- a hanging Bot API never delays trading

def test_hanging_notifications_do_not_delay_the_order():
    fake = StrictMt5(demo=False)
    p = make(fake)
    gate = asyncio.Event()

    async def hang(request):
        await gate.wait()
        return httpx.Response(200, json={"ok": True})

    class Hanging(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await hang(request)

    async def scenario():
        p.notifier = Notifier("TOKEN", 42, transport=Hanging())
        p.test_clock["t"] = NOW + timedelta(seconds=2)
        from owner_setup import CHAT as chat
        from signal_copier.models import IncomingMessage
        await asyncio.wait_for(p.handle_message(IncomingMessage(chat_id=chat, message_id=505, text=XAUUSD_MARKET,
                                                                date=NOW)), 1)
        assert len(fake.positions) == 1  # placed while the first notification is still hanging
        gate.set()
        await p.notifier.aclose()

    asyncio.run(scenario())
