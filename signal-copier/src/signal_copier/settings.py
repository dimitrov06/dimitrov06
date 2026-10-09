"""config.yaml (settings) + .env (secrets)."""

from __future__ import annotations

import os
from decimal import Decimal
from enum import Enum
from pathlib import Path

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
    max_positions_per_symbol: int = 2
    daily_loss_limit_pct: Decimal = Decimal("2.0")
    max_entry_slippage_sl_fraction: Decimal = Decimal("0.3")
    max_signal_age_seconds: int = 60
    max_spread: dict[str, Decimal] = Field(default_factory=dict)
    default_max_spread_sl_fraction: Decimal = Decimal("0.15")


class ExecutorCfg(BaseModel):
    tp_split: str = "equal"
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


def oanda_credentials(settings: Settings, secrets: Secrets) -> OandaCredentials | None:
    """None in record mode. Raises ConfigError if the mode's credentials are missing."""
    if settings.mode is Mode.RECORD:
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
    return settings, secrets
