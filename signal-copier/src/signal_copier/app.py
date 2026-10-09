"""Wires everything together according to MODE."""

from __future__ import annotations

import asyncio
import signal as os_signal

from signal_copier import log
from signal_copier.executor import Executor, OandaClient
from signal_copier.listener import TelegramListener, make_client
from signal_copier.notifier import Notifier
from signal_copier.parser import ParserRules, SignalParser
from signal_copier.pipeline import Pipeline
from signal_copier.risk import RiskManager
from signal_copier.settings import ConfigError, Secrets, Settings, oanda_credentials
from signal_copier.storage import Storage

logger = log.get("app")


def build_pipeline(settings: Settings, secrets: Secrets) -> tuple[Pipeline, OandaClient | None, Notifier]:
    storage = Storage(settings.storage.sqlite_path)
    parser = SignalParser(
        ParserRules.load(settings.parser.rules_file),
        bare_price_entry=settings.parser.bare_price_entry,
        max_price_deviation_pct=settings.parser.max_price_deviation_pct,
    )
    notifier = Notifier(secrets.notify_bot_token, secrets.notify_chat_id)
    creds = oanda_credentials(settings, secrets)
    client = executor = None
    if creds is not None:
        ex = settings.executor
        client = OandaClient(creds, ex.request_timeout_s, ex.retry_attempts, ex.retry_base_delay_s)
        executor = Executor(client, storage, ex.retry_attempts, ex.retry_base_delay_s, ex.pending_order_expiry_minutes)
    pipeline = Pipeline(settings, storage, parser, notifier, RiskManager(settings.risk), client, executor)
    return pipeline, client, notifier


async def _monitor(pipeline: Pipeline, interval: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await pipeline.monitor_once()
        except Exception:
            logger.exception("monitor_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run(settings: Settings, secrets: Secrets) -> None:
    if not (secrets.tg_api_id and secrets.tg_api_hash):
        raise ConfigError("TG_API_ID and TG_API_HASH are required in .env")
    if not settings.telegram.channels:
        raise ConfigError("telegram.channels is empty in config.yaml")

    pipeline, client, notifier = build_pipeline(settings, secrets)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    tg = make_client(secrets.tg_api_id, secrets.tg_api_hash, settings.telegram.session_dir,
                     settings.telegram.session_name)
    listener = TelegramListener(tg, settings.telegram.channels, pipeline.storage, pipeline.handle_message)

    await notifier.send(f"🚀 signal-copier стартира в режим {settings.mode.value}.")
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
                await notifier.send(f"❗ {t.get_name()} спря с грешка: {t.exception()!r}")
                raise t.exception()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await notifier.send("⏹ signal-copier спря.")
        if client is not None:
            await client.aclose()
        await notifier.aclose()
        pipeline.storage.close()
