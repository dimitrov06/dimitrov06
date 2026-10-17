"""python -m signal_copier [run|login|check] [--config config.yaml] [--env .env]"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from signal_copier import log
from signal_copier.settings import ConfigError, load, mt5_check_credentials, oanda_credentials

# after a crash (MT5 terminal not up yet at boot, network down, ...) `run` starts again after these delays;
# a config error (wrong account type, Algo Trading off, not logged in to Telegram) stays fatal
RESTART_DELAYS_S = (10, 30, 60, 120, 300)
STABLE_RUN_S = 600  # a run this long resets the delay
_sleep = time.sleep  # tests replace it


async def _login(settings, secrets) -> None:
    from telethon import utils

    from signal_copier.listener import make_client, resolve_channel

    client = make_client(secrets.tg_api_id, secrets.tg_api_hash, settings.telegram.session_dir,
                         settings.telegram.session_name)
    await client.start(phone=secrets.tg_phone)  # asks for the login code (and 2FA password) in the terminal
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (id {me.id}). Session saved in {settings.telegram.session_dir}/")
    for ch in settings.telegram.channels:
        entity = await resolve_channel(client, ch)
        print(f"  channel ok: {ch} -> {getattr(entity, 'title', entity)} (id {utils.get_peer_id(entity)})")
    await client.disconnect()


async def _check(settings, secrets) -> None:
    from signal_copier.executor import OandaClient

    print(f"mode: {settings.mode.value}, broker: {settings.broker.value}")
    if (mcreds := mt5_check_credentials(settings, secrets)) is not None:
        from signal_copier.executor.mt5 import Mt5Client

        client = Mt5Client(mcreds, settings.mt5)
        try:
            acc = await client.connect()
            a = await client.account_summary()
            print(f"MT5 ok: {acc['login']} @ {acc['server']} ({'demo' if acc['demo'] else 'REAL'}), "
                  f"balance {a['balance']} {a['currency']}, equity {a['NAV']}")
            blockers = await client.trade_blockers()  # paper/live: connect() already refused these
            if blockers is None or blockers:
                print(f"  trading BLOCKED: {'; '.join(blockers) if blockers else 'MT5 did not answer'}")
            else:
                print("  trading: allowed (Algo Trading on, hedging account)")
            for inst in ("XAU_USD", "EUR_USD", "US30_USD"):
                try:
                    bid, ask, _ = await client.pricing(inst)
                    print(f"  {inst} -> {client.symbol(inst)}: {bid} / {ask}")
                except Exception as e:
                    print(f"  {inst} -> {client.symbol(inst)}: {e}")
        finally:
            await client.aclose()
        return
    creds = oanda_credentials(settings, secrets)
    if creds is None:
        print("record mode: broker not used")
        return
    client = OandaClient(creds)
    try:
        a = await client.account_summary()
        print(f"OANDA ok: account {creds.account_id}, balance {a['balance']} {a['currency']}, NAV {a['NAV']}")
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_copier")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "login", "check"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args(argv)
    try:
        settings, secrets = load(args.config, args.env)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    log.setup(settings.logging.level, settings.logging.json_logs)
    if args.command == "login":
        asyncio.run(_login(settings, secrets))
    elif args.command == "check":
        try:
            asyncio.run(_check(settings, secrets))
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2
    else:
        return _run_forever(settings, secrets)
    return 0


def _run_forever(settings, secrets) -> int:
    """Windows Task Scheduler does not restart a task that exits with an error, so the bot does it itself."""
    from signal_copier.app import run

    logger = log.get("main")
    failures = 0
    while True:
        started = time.monotonic()
        try:
            asyncio.run(run(settings, secrets))
            return 0  # stopped on purpose (Ctrl+C / SIGTERM)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130
        except Exception as e:
            if time.monotonic() - started >= STABLE_RUN_S:
                failures = 0
            delay = RESTART_DELAYS_S[min(failures, len(RESTART_DELAYS_S) - 1)]
            failures += 1
            logger.exception("run_failed_restarting", delay_s=delay, attempt=failures)
            print(f"signal-copier stopped with an error ({e!r}); restarting in {delay} s", file=sys.stderr)
            _sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
