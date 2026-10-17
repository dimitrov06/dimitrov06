"""config.yaml (settings) + .env (secrets)."""

from __future__ import annotations

import os
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

OANDA_URLS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}


class Mode(str, Enum):
    RECORD = "record"
    PAPER = "paper"
    LIVE = "live"


class Broker(str, Enum):
    MT5 = "mt5"  # IC Markets via MetaTrader 5 terminal (Windows)
    OANDA = "oanda"


class Mt5Cfg(BaseModel):
    # canonical instrument (parser output) -> broker symbol; anything missing: XAU_USD -> XAUUSD
    symbol_map: dict[str, str] = Field(default_factory=lambda: {
        "US30_USD": "US30", "NAS100_USD": "USTEC", "SPX500_USD": "US500", "DE30_EUR": "DE40",
        "UK100_GBP": "UK100", "WTICO_USD": "XTIUSD", "BCO_USD": "XBRUSD",
    })
    symbol_suffix: str = ""  # e.g. ".a" if the account's symbols carry a suffix
    magic: int = 770077  # marks the bot's own positions in MT5
    deviation_points: int = 20  # max slippage the terminal accepts on market orders
    init_timeout_ms: int = 60000


class TelegramCfg(BaseModel):
    channels: list[str | int] = Field(default_factory=list)
    session_name: str = "signal_copier"
    session_dir: str = "data"


class ParserCfg(BaseModel):
    rules_file: str | None = None
    bare_price_entry: str = "market"
    max_price_deviation_pct: float = 10.0


class RiskCfg(BaseModel):
    risk_per_trade_pct: Decimal = Decimal("0.5")
    max_open_positions: int = 5
    # False: only open positions count towards max_open_positions, pending LIMIT/STOP orders do not
    # (a pending order that fills later can take the count above the limit). Per symbol they always count.
    count_pending_in_max_open: bool = False
    max_positions_per_symbol: int = 2
    daily_loss_limit_pct: Decimal = Decimal("2.0")
    # a new trade risks at most what is left of the daily limit (smaller size; rejected below the minimum)
    cap_to_daily_budget: bool = True
    max_entry_slippage_sl_fraction: Decimal = Decimal("0.3")
    # late entry after an adverse move: size as if the SL were at least this fraction of the channel's SL
    # distance (at most ~3x the normal size), so an SL a few points away cannot blow up the lot size; up to that
    # point the risk stays the full risk_per_trade_pct, as the RTED manual's late entry expects
    late_entry_min_sl_fraction: Decimal = Decimal("0.33")
    max_signal_age_seconds: int = 60
    max_spread: dict[str, Decimal] = Field(default_factory=dict)
    default_max_spread_sl_fraction: Decimal = Decimal("0.15")
    # round-turn commission in account currency per lot (MT5) / per unit (OANDA); 0 on a no-commission account
    commission_per_lot: Decimal = Decimal(0)
    max_lots: dict[str, Decimal] = Field(default_factory=dict)  # per instrument: a bigger size is rejected


class ExecutorCfg(BaseModel):
    # "last": one trade with the last TP (RTED manual: TP2 is the target, TP1 only an observation level)
    # "equal": one trade per TP, size split equally
    tp_split: Literal["last", "equal"] = "last"
    retry_attempts: int = 4
    retry_base_delay_s: float = 1.0
    request_timeout_s: float = 10.0
    pending_order_expiry_minutes: int = 240
    follow_up_symbol_lookback_hours: int = 24
    monitor_interval_s: int = 30


class StorageCfg(BaseModel):
    sqlite_path: str = "data/signal_copier.sqlite"


class LoggingCfg(BaseModel):
    level: str = "INFO"
    json_logs: bool = Field(default=True, alias="json")


class Settings(BaseModel):
    mode: Mode = Mode.RECORD
    broker: Broker = Broker.MT5
    mt5: Mt5Cfg = Field(default_factory=Mt5Cfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    parser: ParserCfg = Field(default_factory=ParserCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    executor: ExecutorCfg = Field(default_factory=ExecutorCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)


class Secrets(BaseModel):
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_phone: str | None = None
    notify_bot_token: str | None = None
    notify_chat_id: int | None = None
    oanda_practice_token: str | None = None
    oanda_practice_account_id: str | None = None
    oanda_live_token: str | None = None
    oanda_live_account_id: str | None = None
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None  # terminal64.exe; optional if MT5 is installed in the default place
    live_confirm: str = "no"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Secrets:
        env = os.environ if env is None else env
        values = {k: env.get(k.upper()) or None for k in cls.model_fields}
        values["live_confirm"] = (env.get("LIVE_CONFIRM") or "no").strip().lower()
        return cls(**{k: v for k, v in values.items() if v is not None})


class ConfigError(RuntimeError):
    pass


class OandaCredentials(BaseModel):
    base_url: str
    token: str
    account_id: str


class Mt5Credentials(BaseModel):
    # All three empty: use the account the MT5 terminal on this PC is already logged in to.
    login: int | None = None
    password: str | None = None
    server: str | None = None
    path: str | None = None
    expect_demo: bool | None  # paper -> must be demo, live -> must be real, None -> read-only check, either


def mt5_credentials(settings: Settings, secrets: Secrets) -> Mt5Credentials | None:
    """None in record mode or for another broker. Raises ConfigError if something is missing."""
    if settings.mode is Mode.RECORD or settings.broker is not Broker.MT5:
        return None
    if settings.mode is Mode.LIVE and secrets.live_confirm != "yes":
        raise ConfigError("MODE=live requires LIVE_CONFIRM=yes in .env")
    return _mt5_creds(secrets, expect_demo=settings.mode is Mode.PAPER)


def mt5_check_credentials(settings: Settings, secrets: Secrets) -> Mt5Credentials | None:
    """For `check`: also in record mode (read-only), without the demo/real requirement."""
    if settings.broker is not Broker.MT5:
        return None
    if settings.mode is not Mode.RECORD:
        return mt5_credentials(settings, secrets)
    return _mt5_creds(secrets, expect_demo=None)


def _mt5_creds(secrets: Secrets, expect_demo: bool | None) -> Mt5Credentials:
    given = [secrets.mt5_login, secrets.mt5_password, secrets.mt5_server]
    if any(given) and not all(given):
        raise ConfigError("MT5_LOGIN, MT5_PASSWORD, MT5_SERVER in .env: fill in all three, or leave all three empty "
                          "to use the account the MT5 terminal is logged in to")
    return Mt5Credentials(login=secrets.mt5_login, password=secrets.mt5_password, server=secrets.mt5_server,
                          path=secrets.mt5_path, expect_demo=expect_demo)


def oanda_credentials(settings: Settings, secrets: Secrets) -> OandaCredentials | None:
    """None in record mode or for another broker. Raises ConfigError if the mode's credentials are missing."""
    if settings.mode is Mode.RECORD or settings.broker is not Broker.OANDA:
        return None
    if settings.mode is Mode.LIVE:
        if secrets.live_confirm != "yes":
            raise ConfigError("MODE=live requires LIVE_CONFIRM=yes in .env")
        token, account, env = secrets.oanda_live_token, secrets.oanda_live_account_id, "live"
    else:
        token, account, env = secrets.oanda_practice_token, secrets.oanda_practice_account_id, "practice"
    if not token or not account:
        raise ConfigError(f"MODE={settings.mode.value} needs OANDA_{env.upper()}_TOKEN and _ACCOUNT_ID in .env")
    return OandaCredentials(base_url=OANDA_URLS[env], token=token, account_id=account)


def load(config_path: str | Path = "config.yaml", env_path: str | Path | None = ".env") -> tuple[Settings, Secrets]:
    if env_path and Path(env_path).exists():
        load_dotenv(env_path, override=False)
    data = {}
    if Path(config_path).exists():
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    settings = Settings.model_validate(data)
    secrets = Secrets.from_env()
    oanda_credentials(settings, secrets)  # fail fast on bad mode/secrets
    mt5_credentials(settings, secrets)
    return settings, secrets
