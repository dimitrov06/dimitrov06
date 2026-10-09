"""python -m signal_copier [run|login|check] [--config config.yaml] [--env .env]"""

from __future__ import annotations

import argparse
import asyncio
import sys

from signal_copier import log
from signal_copier.settings import ConfigError, load, oanda_credentials


async def _login(settings, secrets) -> None:
    from signal_copier.listener import make_client

    client = make_client(secrets.tg_api_id, secrets.tg_api_hash, settings.telegram.session_dir,
                         settings.telegram.session_name)
    await client.start(phone=secrets.tg_phone)  # asks for the login code (and 2FA password) in the terminal
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (id {me.id}). Session saved in {settings.telegram.session_dir}/")
    for ch in settings.telegram.channels:
        entity = await client.get_entity(ch)
        print(f"  channel ok: {ch} -> {getattr(entity, 'title', entity)}")
    await client.disconnect()


async def _check(settings, secrets) -> None:
    from signal_copier.executor import OandaClient

    print(f"mode: {settings.mode.value}")
    creds = oanda_credentials(settings, secrets)
    if creds is None:
        print("record mode: OANDA not used")
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
        asyncio.run(_check(settings, secrets))
    else:
        from signal_copier.app import run

        try:
            asyncio.run(run(settings, secrets))
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
