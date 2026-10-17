"""Wires everything together according to MODE."""

from __future__ import annotations

import asyncio
import json
import signal as os_signal
from datetime import datetime, timedelta, timezone

from signal_copier import log
from signal_copier.executor import Executor, OandaClient
from signal_copier.executor.mt5 import Mt5Client, Mt5Executor
from signal_copier.listener import TelegramListener, make_client
from signal_copier.notifier import Notifier
from signal_copier.parser import ParserRules, SignalParser
from signal_copier.pipeline import Pipeline
from signal_copier.risk import RiskManager
from signal_copier.settings import ConfigError, Secrets, Settings, mt5_credentials, oanda_credentials
from signal_copier.storage import Storage

logger = log.get("app")


def build_pipeline(settings: Settings, secrets: Secrets) -> tuple[Pipeline, OandaClient | None, Notifier]:
    rules = ParserRules.load(settings.parser.rules_file)  # a refused rules file leaves no database open
    storage = Storage(settings.storage.sqlite_path)
    parser = SignalParser(
        rules,
        bare_price_entry=settings.parser.bare_price_entry,
        max_price_deviation_pct=settings.parser.max_price_deviation_pct,
    )
    notifier = Notifier(secrets.notify_bot_token, secrets.notify_chat_id)
    ex = settings.executor
    client = executor = None
    if (creds := oanda_credentials(settings, secrets)) is not None:
        client = OandaClient(creds, ex.request_timeout_s, ex.retry_attempts, ex.retry_base_delay_s)
        executor = Executor(client, storage, ex.retry_attempts, ex.retry_base_delay_s, ex.pending_order_expiry_minutes,
                            ex.tp_split)
    elif (mcreds := mt5_credentials(settings, secrets)) is not None:
        client = Mt5Client(mcreds, settings.mt5)
        executor = Mt5Executor(client, storage, ex.retry_attempts, ex.retry_base_delay_s,
                               ex.pending_order_expiry_minutes, ex.tp_split, settings.parser.max_price_deviation_pct,
                               ex.unknown_order_expiry_minutes)
    pipeline = Pipeline(settings, storage, parser, notifier, RiskManager(settings.risk), client, executor)
    return pipeline, client, notifier


STARTUP_ERROR_KEY = "startup_error_reported"  # state: the config error last sent to the owner, and when
STARTUP_ERROR_REMIND = timedelta(hours=24)


async def _tell_startup_error(notifier: Notifier, sqlite_path: str, e: ConfigError,
                              storage: Storage | None = None) -> None:
    """A config error stops the bot (exit 2), and whatever starts it again (the Task Scheduler .bat, systemd) hits
    the same error seconds later: it is sent once, and again once a day while it lasts. Closes the notifier."""
    text = f"❗ signal-copier не стартира: {e}"
    now = datetime.now(timezone.utc)
    own = None
    told = False
    try:
        if storage is None:
            storage = own = Storage(sqlite_path)
        last = json.loads(storage.get_state(STARTUP_ERROR_KEY) or "null")
        told = bool(last) and last["text"] == text and now - datetime.fromisoformat(last["at"]) < STARTUP_ERROR_REMIND
    except Exception:  # the database is not usable: tell the owner every time rather than never
        logger.exception("startup_error_state_unavailable")
        storage = None
    try:
        if told:
            logger.error("startup_error_already_reported", error=str(e))
            return
        await notifier.send(text + "\nБотът е спрян. При рестарт със същата грешка не го повтарям, напомням веднъж "
                                   "на ден.")
        await notifier.aclose()  # waits for the Bot API: only a message that went out counts as told
        if storage is not None and not getattr(notifier, "failed", 0):
            storage.set_state(STARTUP_ERROR_KEY, json.dumps({"text": text, "at": now.isoformat()}))
    finally:
        await notifier.aclose()
        if own is not None:
            own.close()


MONITOR_ALERT_AFTER = 3  # monitor rounds in a row that fail (MT5 terminal closed / not answering) before a notice
MONITOR_RESTART_AFTER = 10  # ... before the bot restarts, which initializes MT5 again (about 5 min at 30 s)


async def _monitor(pipeline: Pipeline, interval: int, stop: asyncio.Event) -> None:
    failures = 0
    while not stop.is_set():
        try:
            await pipeline.monitor_once()
        except Exception as e:
            failures += 1
            logger.exception("monitor_failed", rounds=failures)
            if failures == MONITOR_ALERT_AFTER:
                await pipeline.notifier.send(
                    f"❗ Брокерът не отговаря ({failures} проверки подред): {e!r}. Не виждам изпълнения, затваряния и "
                    "дневния стоп, и не мога да търгувам. Провери MT5 терминала; ако не се оправи, ботът ще се "
                    "рестартира сам.")
            if failures >= MONITOR_RESTART_AFTER:
                raise RuntimeError(f"monitor failed {failures} rounds in a row: {e!r}") from e
        else:
            if failures >= MONITOR_ALERT_AFTER:
                await pipeline.notifier.send("✅ Брокерът отговаря отново.")
            failures = 0
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run(settings: Settings, secrets: Secrets) -> None:
    if not (secrets.tg_api_id and secrets.tg_api_hash):
        raise ConfigError("TG_API_ID and TG_API_HASH are required in .env")
    if not settings.telegram.channels:
        raise ConfigError("telegram.channels is empty in config.yaml")

    try:
        pipeline, client, notifier = build_pipeline(settings, secrets)
    except ConfigError as e:  # e.g. a refused custom parser.rules_file: the bot stops, so the owner must hear of it
        await _tell_startup_error(Notifier(secrets.notify_bot_token, secrets.notify_chat_id),
                                  settings.storage.sqlite_path, e)
        raise
    if isinstance(client, Mt5Client):
        try:  # also refuses a real account in paper mode, a demo account in live mode and a terminal that can't trade
            await client.connect()
        except Exception as e:
            if isinstance(e, ConfigError):  # exit 2: told once, not on every start of the .bat
                await _tell_startup_error(notifier, settings.storage.sqlite_path, e, pipeline.storage)
            else:  # restarted in-process with growing delays
                await notifier.send(f"❗ signal-copier не стартира: {e}")
                await notifier.aclose()
            await client.aclose()  # the restart loop builds everything again
            pipeline.storage.close()
            raise
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    broker = "" if client is None else f", брокер {settings.broker.value}"

    async def started() -> None:  # logged in to Telegram, every channel found: a start that works
        pipeline.storage.set_state(STARTUP_ERROR_KEY, None)  # the next config error is news again
        await notifier.send(f"🚀 signal-copier стартира в режим {settings.mode.value}{broker}.")

    tg = make_client(secrets.tg_api_id, secrets.tg_api_hash, settings.telegram.session_dir,
                     settings.telegram.session_name)
    listener = TelegramListener(tg, settings.telegram.channels, pipeline.storage, pipeline.handle_message,
                                notify=notifier.send, on_ready=started)
    startup_error: ConfigError | None = None
    tasks = [
        asyncio.create_task(listener.run(stop), name="listener"),
        asyncio.create_task(notifier.poll_commands(pipeline.handle_command, stop), name="commands"),
    ]
    if client is not None:
        tasks.append(asyncio.create_task(_monitor(pipeline, settings.executor.monitor_interval_s, stop),
                                         name="monitor"))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception():
                if isinstance(t.exception(), ConfigError) and not listener.ready:
                    # Telegram not logged in, the channel gone: exit 2, and the .bat starts it again in 30 s
                    startup_error = t.exception()
                else:
                    await notifier.send(f"❗ {t.get_name()} спря с грешка: {t.exception()!r}")
                raise t.exception()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        if startup_error is not None:  # told once, then once a day (no 🚀 was sent, so no ⏹ either)
            await _tell_startup_error(notifier, settings.storage.sqlite_path, startup_error, pipeline.storage)
        else:
            await notifier.send("⏹ signal-copier спря.")
        if client is not None:
            await client.aclose()
        await notifier.aclose()
        pipeline.storage.close()
