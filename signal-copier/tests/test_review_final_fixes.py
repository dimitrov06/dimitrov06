"""Review of 34ef811 (final review fixes) on the owner's real-money setup (fake MT5, fake Telethon).

- a command written into a signal post that failed (❗) is tried again by a later edit, and a corrected
  "move SL to <price>" in the post is applied (raw-text-update-hides-in-post-commands);
- "Close now" written under the new signal of a "Close XAUUSD + new XAUUSD signal" post closes that new trade, and
  the close of the older trade is never taken for it (left-same-message-key-collision);
- a command rejected for a wrong symbol and corrected by removing the symbol runs
  (follow-ups-for-treats-rejected-as-processed);
- a size written after a comma in a close sentence is never a silent full close
  (before-size-comma-turns-partial-into-full-close);
- a custom rules file with a YAML or regex error, or a wrong path, stops the bot with a notice
  (broken-custom-rules-file-silent-restart-loop);
- a config error at start is told once, not on every restart by the .bat (configerror-notice-storm-with-bat);
- an error while the listener starts still disconnects Telethon
  (catchup-region-exception-skips-telegram-disconnect).
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import signal_copier.__main__ as main_mod
import signal_copier.app as app
from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import XAUUSD_CLOSE, XAUUSD_MARKET, XAUUSD_SELL_LIMIT, bot_positions, edit, events, make, post
from signal_copier.listener.telegram import TelegramListener
from signal_copier.models import IncomingMessage, ParseStatus
from signal_copier.parser import ParserRules, SignalParser
from signal_copier.settings import ConfigError, Secrets, Settings
from signal_copier.storage import Storage
from test_resilience import CHAT, TITLE, FakeTg, entity, fake_telethon, session, tg  # noqa: F401 (fixture)

T0 = NOW


def decisions(p, kind="follow_up"):
    return [d for d, _ in events(p, kind)]


# ------------------------------------------------- raw-text-update-hides-in-post-commands

def test_failed_cancel_in_post_is_retried_by_a_later_edit():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    fake.ipc_down = True  # MT5 does not answer while the channel cancels: ❗
    edit(p, XAUUSD_SELL_LIMIT + "\n\nCANCELLED ❌", 800, T0 + timedelta(minutes=20))
    fake.ipc_down = False
    assert p.storage.get_follow_up("-1001:800:CANCEL:XAU_USD")["status"] == "FAILED"
    assert len(fake.orders) == 1
    edit(p, XAUUSD_SELL_LIMIT + "\n\nCANCELLED ❌❌", 800, T0 + timedelta(minutes=25))
    assert fake.orders == {}  # the order the channel cancelled can no longer fill
    assert decisions(p)[-2:] == ["RETRY", "EXECUTED"]
    assert "отменена чакаща поръчка" in p.notifier.sent[-1]


def test_failed_close_in_post_is_retried_by_a_later_edit():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    fake.fail = {"positions_get": 1}
    edit(p, XAUUSD_MARKET + "\n\nClose now", 505, T0 + timedelta(minutes=10))
    assert p.storage.get_follow_up("-1001:505:CLOSE:XAU_USD")["status"] == "FAILED"
    edit(p, XAUUSD_MARKET + "\n\nClose now ✅", 505, T0 + timedelta(minutes=12))
    assert bot_positions(fake) == []
    assert decisions(p)[-2:] == ["RETRY", "EXECUTED"]


def test_executed_partial_close_in_post_never_runs_again_on_later_edits():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    full = pos.volume
    edit(p, XAUUSD_MARKET + "\n\nClose half now", 505, T0 + timedelta(minutes=10))
    half = pos.volume
    assert half < full
    edit(p, XAUUSD_MARKET + "\n\nClose half now ✅", 505, T0 + timedelta(minutes=12))
    edit(p, XAUUSD_MARKET + "\n\nClose half now ✅✅", 505, T0 + timedelta(minutes=14))
    assert pos.volume == half
    assert decisions(p) == ["EXECUTED"]


def test_failed_partial_close_in_post_is_not_retried_by_a_later_edit():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    full = pos.volume
    fake.fail = {"positions_get": 1}
    edit(p, XAUUSD_MARKET + "\n\nClose half now", 505, T0 + timedelta(minutes=10))
    assert p.storage.get_follow_up("-1001:505:CLOSE_PARTIAL:XAU_USD")["status"] == "FAILED"
    edit(p, XAUUSD_MARKET + "\n\nClose half now ✅", 505, T0 + timedelta(minutes=12))
    assert pos.volume == full  # a partial close is never run on a guess that the first one did nothing
    assert decisions(p) == ["FAILED"]


@pytest.mark.parametrize("line", ["TP1 hit ✅ move SL to {}", "Move SL to {}"])
def test_corrected_sl_price_in_post_is_applied(line):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    fake.ticks["XAUUSD"] = (4492.0, 4492.3)
    edit(p, XAUUSD_MARKET + "\n\n" + line.format("4480.00"), 505, T0 + timedelta(minutes=10))
    assert pos.sl == 4480.00
    edit(p, XAUUSD_MARKET + "\n\n" + line.format("4485.00"), 505, T0 + timedelta(minutes=12))
    assert pos.sl == 4485.00
    assert "4485" in p.notifier.sent[-1]
    n = len(events(p, "follow_up"))
    edit(p, XAUUSD_MARKET + "\n\n" + line.format("4485.00") + " ✅", 505, T0 + timedelta(minutes=14))
    assert len(events(p, "follow_up")) == n  # the same price again is not a new command
    assert pos.sl == 4485.00


# ------------------------------------------------- left-same-message-key-collision

COMBO = XAUUSD_CLOSE + "\n\n" + XAUUSD_MARKET.replace("4479.69", "4479.80")  # close the old trade + a new signal


def test_close_under_the_new_signal_of_a_close_plus_signal_post_closes_that_trade():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)  # A
    (a,) = bot_positions(fake)
    post(p, COMBO, 520, T0 + timedelta(minutes=30))  # closes A, opens B
    (b,) = bot_positions(fake)
    assert b is not a
    edit(p, COMBO + "\n\nClose now", 520, T0 + timedelta(minutes=50), posted=T0 + timedelta(minutes=30))
    assert bot_positions(fake) == []  # B, which the channel closed, is closed
    assert p.storage.get_follow_up("-1001:520:CLOSE:XAU_USD")["signal_key"] == "-1001:505:0"  # A's row is kept
    assert sum("вече е обработена" in t for t in p.notifier.sent) == 1  # only A's close, edited
    assert any(t.startswith("🔧 CLOSE за XAU_USD BUY (msg 520)") for t in p.notifier.sent)
    # yet another edit: neither close runs again
    edit(p, COMBO + "\n\nClose now ✅", 520, T0 + timedelta(minutes=55), posted=T0 + timedelta(minutes=30))
    assert decisions(p)[-1] == "DUPLICATE" and decisions(p).count("EXECUTED") == 2


def test_close_under_the_new_signal_runs_when_the_first_close_found_no_trade():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, COMBO, 520, T0)  # no older gold trade: that close is rejected; B opens
    assert p.storage.get_follow_up("-1001:520:CLOSE:XAU_USD")["status"] == "REJECTED"
    assert len(bot_positions(fake)) == 1
    edit(p, COMBO + "\n\nClose now", 520, T0 + timedelta(minutes=20))
    assert bot_positions(fake) == []


def test_close_added_above_a_signal_that_was_closed_in_its_post_closes_the_older_trade():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)  # A
    (a,) = bot_positions(fake)
    b_post = XAUUSD_MARKET.replace("4479.69", "4479.80")
    post(p, b_post, 520, T0 + timedelta(minutes=30))  # B
    edit(p, b_post + "\n\nClose now", 520, T0 + timedelta(minutes=40), posted=T0 + timedelta(minutes=30))
    assert bot_positions(fake) == [a]  # B closed by the command in its own post
    edit(p, XAUUSD_CLOSE + "\n\n" + b_post + "\n\nClose now", 520, T0 + timedelta(minutes=45),
         posted=T0 + timedelta(minutes=30))
    assert bot_positions(fake) == []  # "Close XAUUSD" written above B is about A, not B's close again
    assert not any("вече е обработена" in t for t in p.notifier.sent)


# ------------------------------------------------- follow-ups-for-treats-rejected-as-processed

def test_cancel_corrected_by_dropping_a_wrong_symbol_runs():
    fake = StrictMt5(demo=False)
    fake.ticks["XAUUSD"] = (4150.00, 4150.30)
    p = make(fake)
    post(p, XAUUSD_SELL_LIMIT, 800, T0)
    post(p, "Cancel Pending Order on AUDUSD", 801, T0 + timedelta(minutes=5), reply_to=800)
    assert events(p, "follow_up")[-1][0] == "REJECTED" and len(fake.orders) == 1
    edit(p, "Cancel Pending Order", 801, T0 + timedelta(minutes=6), posted=T0 + timedelta(minutes=5), reply_to=800)
    assert fake.orders == {}
    assert "отменена чакаща поръчка" in p.notifier.sent[-1]


def test_close_corrected_by_dropping_a_wrong_symbol_runs_once():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    post(p, "‼️ Close AUDUSD Manually now at 4485.10!", 506, T0 + timedelta(minutes=5), reply_to=505)
    assert len(bot_positions(fake)) == 1
    edit(p, "‼️ Close Manually now at 4485.10!", 506, T0 + timedelta(minutes=6), posted=T0 + timedelta(minutes=5),
         reply_to=505)
    assert bot_positions(fake) == []
    edit(p, "‼️ Close Manually now at 4485.10! ✅", 506, T0 + timedelta(minutes=7), posted=T0 + timedelta(minutes=5),
         reply_to=505)
    assert decisions(p) == ["REJECTED", "EXECUTED", "DUPLICATE"]


def test_partial_close_corrected_by_dropping_a_wrong_symbol_closes_one_half_only():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    full = pos.volume
    post(p, "Close 50% of AUDUSD now", 506, T0 + timedelta(minutes=5), reply_to=505)
    assert pos.volume == full
    edit(p, "Close 50% now", 506, T0 + timedelta(minutes=6), posted=T0 + timedelta(minutes=5), reply_to=505)
    half = pos.volume
    assert half < full
    edit(p, "Close 50% now ✅", 506, T0 + timedelta(minutes=7), posted=T0 + timedelta(minutes=5), reply_to=505)
    assert pos.volume == half


# ------------------------------------------------- before-size-comma-turns-partial-into-full-close

SIZE_AFTER_COMMA = ["Close XAUUSD now, half position", "Close XAUUSD, 50% now", "Close XAUUSD now, 50% of the position",
                    "‼️ Close XAUUSD Manually now, 50% at 4485.10!", "Close XAUUSD, half",
                    "Close XAUUSD at 4485.10 half position"]


@pytest.mark.parametrize("text", SIZE_AFTER_COMMA)
def test_size_after_a_comma_is_not_guessed(text):
    r = SignalParser().parse(IncomingMessage(chat_id=CHAT, message_id=506, text=text, date=NOW, reply_to_msg_id=505))
    assert r.status is ParseStatus.UNCERTAIN and not r.follow_ups
    assert [i.reason for i in r.issues] == ["unclear_close_command"]


@pytest.mark.parametrize("text", SIZE_AFTER_COMMA[:3])
def test_size_after_a_comma_closes_nothing_and_tells_the_owner(text):
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    full = pos.volume
    post(p, text, 506, T0 + timedelta(minutes=10), reply_to=505)
    assert bot_positions(fake) == [pos] and pos.volume == full
    assert p.notifier.sent[-1].startswith("⚠️ Не разбрах")


def test_size_after_a_comma_written_into_the_post_closes_nothing_and_tells_the_owner():
    fake = StrictMt5(demo=False)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505, T0)
    (pos,) = bot_positions(fake)
    full = pos.volume
    edit(p, XAUUSD_MARKET + "\n\nClose now, half position", 505, T0 + timedelta(minutes=10))
    assert bot_positions(fake) == [pos] and pos.volume == full
    assert ("EDIT_UNCLEAR", None) in events(p, "edit")


@pytest.mark.parametrize("text, fraction", [("Close XAUUSD 50% now", "0.5"), ("Close half now, move SL to BE", "0.5"),
                                            ("Close 30% of XAUUSD now", "0.3")])
def test_size_in_the_close_clause_is_still_a_partial_close(text, fraction):
    r = SignalParser().parse(IncomingMessage(chat_id=CHAT, message_id=506, text=text, date=NOW, reply_to_msg_id=505))
    assert r.status is ParseStatus.FOLLOW_UP and not r.issues
    assert [str(f.fraction) for f in r.follow_ups if f.action.value == "CLOSE_PARTIAL"] == [fraction]


# ------------------------------------------------- broken-custom-rules-file-silent-restart-loop

BUILTIN = (Path(app.__file__).parent / "parser" / "rules.yaml").read_text(encoding="utf-8")


def _rules_file(tmp_path, variant) -> Path:
    path = tmp_path / "my_rules.yaml"
    if variant == "yaml_escape":  # an alias pattern added in double quotes
        path.write_text(BUILTIN.replace("market_words: [", 'market_words: ["\\d+ NOW", '), encoding="utf-8")
    elif variant == "bad_regex":
        path.write_text(BUILTIN.replace("market_words: [", "market_words: ['(NOW', "), encoding="utf-8")
    elif variant == "unknown_macro":
        path.write_text(BUILTIN.replace("command_words: [", "command_words: ['{NOPE}', "), encoding="utf-8")
    elif variant == "empty":
        path.write_text("", encoding="utf-8")
    elif variant == "old_layout":  # yesterday's file without the safety sections
        path.write_text("symbols: {GOLD: XAU_USD}\nfx_currencies: [USD]\n", encoding="utf-8")
    elif variant == "wrong_type":
        path.write_text(BUILTIN.replace("market_words: [", "market_words: 7\nx: ["), encoding="utf-8")
    return path  # missing_file: never written


VARIANTS = ["yaml_escape", "bad_regex", "unknown_macro", "missing_file", "empty", "wrong_type", "old_layout"]


@pytest.mark.parametrize("variant", VARIANTS)
def test_unusable_rules_file_is_a_config_error(tmp_path, variant):
    with pytest.raises(ConfigError, match="parser rules"):
        ParserRules.load(_rules_file(tmp_path, variant))


def test_builtin_rules_still_load():
    assert ParserRules.load().follow_ups


@pytest.fixture
def told(monkeypatch):
    out = []

    class RecordingNotifier:
        def __init__(self, token, chat_id):
            pass

        async def send(self, text):
            out.append(text)

        async def aclose(self):
            pass

    monkeypatch.setattr(app, "Notifier", RecordingNotifier)
    return out


def _settings(tmp_path, rules_file) -> Settings:
    return Settings.model_validate({"mode": "record", "telegram": {"channels": [CHAT]},
                                    "parser": {"rules_file": str(rules_file)},
                                    "storage": {"sqlite_path": str(tmp_path / "bot.db")}})


class Restarted(Exception):
    pass


@pytest.mark.parametrize("variant", ["yaml_escape", "bad_regex", "missing_file"])
def test_unusable_rules_file_stops_the_bot_with_a_notice(tmp_path, monkeypatch, told, variant):
    sleeps = []

    def restart_delay(seconds):  # the restart loop treats it as a crash: fail here instead of looping forever
        sleeps.append(seconds)
        raise Restarted(seconds)

    monkeypatch.setattr(main_mod, "_sleep", restart_delay)
    secrets = Secrets(tg_api_id=1, tg_api_hash="h", notify_bot_token="t", notify_chat_id=1)
    assert main_mod._run_forever(_settings(tmp_path, _rules_file(tmp_path, variant)), secrets) == 2
    assert sleeps == []  # not a crash: no restart loop
    assert len(told) == 1 and told[0].startswith("❗ signal-copier не стартира") and "parser rules" in told[0]


def test_check_reports_an_unusable_rules_file(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"mode: record\nparser:\n  rules_file: '{_rules_file(tmp_path, 'bad_regex')}'\n", encoding="utf-8")
    assert main_mod.main(["check", "--config", str(cfg), "--env", str(tmp_path / "none.env")]) == 2
    assert "parser rules" in capsys.readouterr().err


# ------------------------------------------------- configerror-notice-storm-with-bat

def _start(settings) -> None:
    with pytest.raises(ConfigError):
        asyncio.run(app.run(settings, Secrets(tg_api_id=1, tg_api_hash="h")))


def test_same_config_error_on_every_restart_is_told_once(tmp_path, told):
    settings = _settings(tmp_path, _rules_file(tmp_path, "old_layout"))
    for _ in range(5):  # the .bat starts the bot again every 30 s
        _start(settings)
    assert len(told) == 1


def test_another_config_error_is_told_and_the_same_one_again_after_a_day(tmp_path, told):
    settings = _settings(tmp_path, _rules_file(tmp_path, "old_layout"))
    _start(settings)
    _start(settings)
    assert len(told) == 1
    db = Storage(tmp_path / "bot.db")
    state = json.loads(db.get_state(app.STARTUP_ERROR_KEY))
    state["at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    db.set_state(app.STARTUP_ERROR_KEY, json.dumps(state))
    db.close()
    _start(settings)
    assert len(told) == 2  # a reminder a day later
    _start(_settings(tmp_path, tmp_path / "moved.yaml"))
    assert len(told) == 3 and "moved.yaml" in told[-1]  # a different error is news


class DroppingTg(FakeTg):
    async def run_until_disconnected(self):
        self.running.set()
        await asyncio.sleep(0.05)
        raise ConnectionError("Automatic reconnection failed 5 time(s)")


def test_config_error_is_told_again_after_a_start_that_worked(tmp_path, told, monkeypatch):
    bad = _settings(tmp_path, _rules_file(tmp_path, "old_layout"))
    _start(bad)
    _start(bad)
    assert len(told) == 1
    p = make(StrictMt5(demo=False))  # the owner fixes the file: the bot starts (and later loses Telegram)
    p.storage = p.executor.storage = Storage(tmp_path / "bot.db")
    p.settings.telegram.channels = [CHAT]
    p.settings.executor.monitor_interval_s = 0.05
    with monkeypatch.context() as m:
        m.setattr(app, "build_pipeline", lambda s, sec: (p, p.client, p.notifier))
        m.setattr(app, "make_client", lambda *a: DroppingTg([(entity(), TITLE)], {CHAT: []}))
        with pytest.raises(ConnectionError):
            asyncio.run(asyncio.wait_for(app.run(p.settings, NS(tg_api_id=1, tg_api_hash="h")), 3))
    _start(bad)  # the file is broken again later: the owner hears of it again
    assert len(told) == 2


def test_mt5_config_error_at_start_is_told_once(tmp_path, monkeypatch):
    sent = []

    def build(settings, secrets):
        p = make(StrictMt5(demo=True))  # live mode, demo account: refused at connect, exit 2
        p.storage = p.executor.storage = Storage(tmp_path / "bot.db")
        p.notifier.send = lambda text: _record(sent, text)
        return p, p.client, p.notifier

    monkeypatch.setattr(app, "build_pipeline", build)
    settings = Settings.model_validate({"mode": "live", "telegram": {"channels": [CHAT]},
                                        "storage": {"sqlite_path": str(tmp_path / "bot.db")}})
    for _ in range(3):
        with pytest.raises(ConfigError):
            asyncio.run(app.run(settings, NS(tg_api_id=1, tg_api_hash="h")))
    assert len(sent) == 1 and sent[0].startswith("❗ signal-copier не стартира") and "demo" in sent[0]


async def _record(out, text):
    out.append(text)


# ------------------------------------------------- catchup-region-exception-skips-telegram-disconnect

class CountingTg(FakeTg):
    def __init__(self, *a, fail_on=None, authorized=True):
        super().__init__(*a)
        self.disconnects = 0
        self.fail_on, self.authorized = fail_on, authorized

    async def is_user_authorized(self):
        return self.authorized

    async def get_messages(self, entity, limit=None, min_id=0, reverse=False, ids=None):
        if self.fail_on == "recheck" and ids is not None or self.fail_on == "catch_up" and min_id:
            raise ConnectionError("Automatic reconnection failed 5 time(s)")
        return await super().get_messages(entity, limit, min_id, reverse, ids)

    async def disconnect(self):
        self.disconnects += 1
        await super().disconnect()


def _restart(p, history, **kw):
    client = CountingTg([(entity(), TITLE)], history, **kw)
    lis = TelegramListener(client, [CHAT], p.storage, p.handle_message, notify=p.notifier.send,
                           clock=lambda: NOW + timedelta(minutes=5))
    return client, lis


@pytest.mark.parametrize("fail_on", ["recheck", "catch_up"])
def test_error_while_the_listener_starts_disconnects_telethon(fail_on):
    p = make(StrictMt5(demo=False))
    p.test_clock["t"] = NOW + timedelta(seconds=2)
    history = {CHAT: []}
    asyncio.run(session(p, history, [tg(1520, XAUUSD_MARKET)]))  # a live signal: the next start reads its post again
    history[CHAT].append(tg(1521, "Good morning traders", date=NOW + timedelta(minutes=3)))  # missed while down
    client, lis = _restart(p, history, fail_on=fail_on)
    with pytest.raises(ConnectionError):
        asyncio.run(lis.run(asyncio.Event()))
    assert client.disconnects == 1  # the session file is closed before the restart opens it again


def test_session_not_logged_in_disconnects_telethon():
    p = make(StrictMt5(demo=False))
    client, lis = _restart(p, {CHAT: []}, authorized=False)
    with pytest.raises(ConfigError):
        asyncio.run(lis.run(asyncio.Event()))
    assert client.disconnects == 1


def test_stopped_listener_disconnects_once():
    p = make(StrictMt5(demo=False))
    client, lis = _restart(p, {CHAT: []})
    stop = asyncio.Event()

    async def go():
        task = asyncio.create_task(lis.run(stop))
        await asyncio.wait_for(client.running.wait(), 2)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    assert client.disconnects == 1
