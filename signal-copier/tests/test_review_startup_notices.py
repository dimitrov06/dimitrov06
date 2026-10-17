"""Review of 93544c7 (rules file errors, config errors told once a day, sizes after a close).

- a rules file that is not UTF-8 or has command_guards as a list is a ConfigError, told on Telegram, never a silent
  restart loop (rules-load-unicode-and-guards-type-escape-configerror);
- Telegram not logged in or the channel gone is told once a day, not 3 messages on every start of the .bat
  (listener-configerror-still-storms-with-bat);
- a startup-error notice cut off by the notifier's drain timeout is not stored as told
  (dropped-startup-notice-counted-as-told);
- "Close XAUUSD 100% now" is a full close, as before 93544c7 (close-100-percent-now-uncertain).
"""

import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest

import signal_copier.__main__ as main_mod
import signal_copier.app as app
import signal_copier.notifier.bot as bot
from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import make
from signal_copier.models import FollowUpAction, IncomingMessage, ParseStatus
from signal_copier.notifier import Notifier
from signal_copier.parser import ParserRules, SignalParser
from signal_copier.settings import ConfigError, Secrets, Settings
from signal_copier.storage import Storage
from test_resilience import CHAT, TITLE, FakeTg, entity, fake_telethon  # noqa: F401 (fixture)
from test_review_final_fixes import BUILTIN, Restarted, _settings, told  # noqa: F401 (fixture)


# ------------------------------------------------- rules-load-unicode-and-guards-type-escape-configerror

def _bulgarian_rules(tmp_path, encoding):
    path = tmp_path / "my_rules.yaml"
    text = BUILTIN.replace("symbols:\n", "symbols:\n  ЗЛАТО: XAU_USD   # злато\n")
    path.write_bytes(text.encode(encoding, errors="replace"))  # Notepad writes "?" for the emojis it cannot keep
    return path


@pytest.mark.parametrize("encoding", ["cp1251", "utf-16"])  # Notepad "ANSI" and "Unicode"
def test_rules_file_not_in_utf8_is_a_config_error(tmp_path, encoding):
    with pytest.raises(ConfigError, match="parser rules .* UTF-8"):
        ParserRules.load(_bulgarian_rules(tmp_path, encoding))


def test_rules_file_in_utf8_with_bulgarian_alias_loads(tmp_path):
    rules = ParserRules.load(_bulgarian_rules(tmp_path, "utf-8"))
    assert rules.raw.symbols["ЗЛАТО"] == "XAU_USD"


@pytest.mark.parametrize("guards", ["command_guards:\n  - negation: x\n", "command_guards: '\\bNOT\\b'\n"])
def test_command_guards_not_a_mapping_is_a_config_error(tmp_path, guards):
    path = tmp_path / "my_rules.yaml"
    head, _, tail = BUILTIN.partition("command_guards:\n")
    path.write_text(head + guards + tail[tail.index("\n# Words of a command"):], encoding="utf-8")
    with pytest.raises(ConfigError, match="parser rules .*command_guards"):
        ParserRules.load(path)


def test_rules_file_not_in_utf8_stops_the_bot_with_a_notice(tmp_path, monkeypatch, told):
    sleeps = []

    def restart_delay(seconds):  # a crash would restart in-process forever: fail here instead
        sleeps.append(seconds)
        raise Restarted(seconds)

    monkeypatch.setattr(main_mod, "_sleep", restart_delay)
    secrets = Secrets(tg_api_id=1, tg_api_hash="h", notify_bot_token="t", notify_chat_id=1)
    assert main_mod._run_forever(_settings(tmp_path, _bulgarian_rules(tmp_path, "cp1251")), secrets) == 2
    assert sleeps == []
    assert len(told) == 1 and told[0].startswith("❗ signal-copier не стартира") and "UTF-8" in told[0]


# ------------------------------------------------- listener-configerror-still-storms-with-bat

class NotLoggedIn(FakeTg):
    async def is_user_authorized(self):
        return False


class Ended(FakeTg):
    """Telethon after 'Automatic reconnection failed': run_until_disconnected ends with an error."""

    async def run_until_disconnected(self):
        self.running.set()
        await asyncio.sleep(0.05)
        raise ConnectionError("Automatic reconnection failed 5 time(s)")


def _live_bot(tmp_path, monkeypatch, telegram, channels=(CHAT,)):
    sent = []

    def build(settings, secrets):
        p = make(StrictMt5(demo=False))
        p.storage = p.executor.storage = Storage(tmp_path / "bot.db")
        p.settings.telegram.channels = list(channels)
        p.settings.executor.monitor_interval_s = 0.05

        async def record(text):
            sent.append(text)
        p.notifier.send = record
        return p, p.client, p.notifier

    monkeypatch.setattr(app, "build_pipeline", build)
    monkeypatch.setattr(app, "make_client", lambda *a: telegram())
    settings = Settings.model_validate({"mode": "live", "telegram": {"channels": list(channels)},
                                        "storage": {"sqlite_path": str(tmp_path / "bot.db")}})
    return settings, sent


def _start(settings, error=ConfigError):
    with pytest.raises(error):
        asyncio.run(asyncio.wait_for(app.run(settings, NS(tg_api_id=1, tg_api_hash="h")), 3))


def test_telegram_not_logged_in_is_told_once_not_on_every_restart(tmp_path, monkeypatch):
    settings, sent = _live_bot(tmp_path, monkeypatch, lambda: NotLoggedIn([(entity(), TITLE)], {CHAT: []}))
    for _ in range(3):  # three starts by the .bat
        _start(settings)
    assert len(sent) == 1, sent  # no 🚀 / "❗ listener спря" / ⏹ on every start
    assert sent[0].startswith("❗ signal-copier не стартира") and "not logged in" in sent[0]


def test_channel_gone_is_told_once_not_on_every_restart(tmp_path, monkeypatch):
    name = "RTED Investing"
    db = Storage(tmp_path / "bot.db")
    db.set_state("channel_peer:rted investing", str(CHAT))  # matched by name on an earlier start
    db.close()
    other = NS(peer_id=-1002, title="RTED Investing Free")
    settings, sent = _live_bot(tmp_path, monkeypatch, lambda: FakeTg([(other, other.title)], {}), channels=[name])
    for _ in range(3):  # the owner was removed from the paid group
        _start(settings)
    assert len(sent) == 1, sent
    assert sent[0].startswith("❗ signal-copier не стартира") and "no longer among your Telegram chats" in sent[0]


def test_start_that_works_is_announced_and_makes_the_next_telegram_error_news(tmp_path, monkeypatch):
    settings, sent = _live_bot(tmp_path, monkeypatch, lambda: NotLoggedIn([(entity(), TITLE)], {CHAT: []}))
    _start(settings)
    _start(settings)
    assert len(sent) == 1
    monkeypatch.setattr(app, "make_client", lambda *a: Ended([(entity(), TITLE)], {CHAT: []}))
    _start(settings, ConnectionError)  # the owner logged in again: it starts (and later loses the connection)
    rocket = next(i for i, t in enumerate(sent) if t.startswith("🚀"))
    listening = next(i for i, t in enumerate(sent) if t.startswith("👂"))
    assert rocket < listening
    assert any("listener спря с грешка" in t for t in sent) and sent[-1] == "⏹ signal-copier спря."
    monkeypatch.setattr(app, "make_client", lambda *a: NotLoggedIn([(entity(), TITLE)], {CHAT: []}))
    before = len(sent)
    _start(settings)  # logged out again later: the owner hears of it again
    assert len(sent) == before + 1 and "not logged in" in sent[-1]


def test_telegram_down_at_start_is_still_reported_as_a_crash(tmp_path, monkeypatch):
    class NoNetwork(FakeTg):
        async def connect(self):
            raise ConnectionError("Connection to Telegram failed 5 time(s)")

    settings, sent = _live_bot(tmp_path, monkeypatch, lambda: NoNetwork([(entity(), TITLE)], {CHAT: []}))
    _start(settings, ConnectionError)  # _run_forever restarts on it with growing delays
    assert any("listener спря с грешка" in t for t in sent) and sent[-1] == "⏹ signal-copier спря."


# ------------------------------------------------- dropped-startup-notice-counted-as-told

def _slow_bot_api(monkeypatch, delivered):
    monkeypatch.setattr(bot, "DRAIN_TIMEOUT_S", 0.2)

    async def slow(request):
        await asyncio.sleep(1)  # slow network at boot: each httpx phase under SEND_TIMEOUT_S, the whole over the drain
        delivered.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(slow)


def test_messages_cut_off_by_the_drain_timeout_count_as_failed(monkeypatch):
    transport = _slow_bot_api(monkeypatch, delivered := [])

    async def go():
        n = Notifier("t", 1, transport=transport)
        for i in range(3):
            await n.send(f"m{i}")
        await n.aclose()
        return n

    n = asyncio.run(go())
    assert delivered == [] and n.failed == 3


def test_dropped_startup_notice_is_not_stored_as_told(tmp_path, monkeypatch):
    transport = _slow_bot_api(monkeypatch, delivered := [])
    monkeypatch.setattr(app, "Notifier", lambda token, chat: Notifier(token, chat, transport=transport))
    bad = tmp_path / "rules.yaml"
    bad.write_text("symbols: {GOLD: XAU_USD}\nfx_currencies: [USD]\n", encoding="utf-8")
    secrets = Secrets(tg_api_id=1, tg_api_hash="h", notify_bot_token="t", notify_chat_id=1)
    with pytest.raises(ConfigError):
        asyncio.run(app.run(_settings(tmp_path, bad), secrets))
    assert delivered == []
    db = Storage(tmp_path / "bot.db")
    assert db.get_state(app.STARTUP_ERROR_KEY) is None  # not told: the next start tries again
    db.close()


# ------------------------------------------------- close-100-percent-now-uncertain

def _parse(text):
    return SignalParser().parse(IncomingMessage(chat_id=CHAT, message_id=600, text=text, date=NOW,
                                                reply_to_msg_id=500))


@pytest.mark.parametrize("text", ["Close XAUUSD 100% now", "Close XAUUSD now 100%", "Close all 100%",
                                  "Close XAUUSD 100 percent now"])
def test_close_100_percent_is_a_full_close(text):
    r = _parse(text)
    assert r.status is ParseStatus.FOLLOW_UP and not r.issues
    assert [(f.action, f.fraction) for f in r.follow_ups] == [(FollowUpAction.CLOSE, None)]


@pytest.mark.parametrize("text", ["Close XAUUSD 100% now, half position", "Close XAUUSD 100%, close 50% later",
                                  "Close XAUUSD 100% now, 50% of the target reached"])
def test_another_size_after_a_close_100_percent_is_still_uncertain(text):
    r = _parse(text)
    assert r.status is ParseStatus.UNCERTAIN
    assert not any(f.action is FollowUpAction.CLOSE for f in r.follow_ups)
