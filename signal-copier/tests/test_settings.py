import pytest

from signal_copier.settings import ConfigError, Mode, Secrets, Settings, load, oanda_credentials


def test_default_mode_is_record(tmp_path):
    settings, _ = load(tmp_path / "missing.yaml", None)
    assert settings.mode is Mode.RECORD
    assert oanda_credentials(settings, Secrets()) is None


def test_live_requires_explicit_confirm():
    s = Settings(mode=Mode.LIVE)
    secrets = Secrets.from_env({"OANDA_LIVE_TOKEN": "x", "OANDA_LIVE_ACCOUNT_ID": "1", "LIVE_CONFIRM": "no"})
    with pytest.raises(ConfigError, match="LIVE_CONFIRM"):
        oanda_credentials(s, secrets)
    secrets = Secrets.from_env({"OANDA_LIVE_TOKEN": "x", "OANDA_LIVE_ACCOUNT_ID": "1", "LIVE_CONFIRM": "YES"})
    assert oanda_credentials(s, secrets).base_url == "https://api-fxtrade.oanda.com"


def test_paper_uses_practice_and_needs_token():
    with pytest.raises(ConfigError):
        oanda_credentials(Settings(mode=Mode.PAPER), Secrets())
    secrets = Secrets.from_env({"OANDA_PRACTICE_TOKEN": "x", "OANDA_PRACTICE_ACCOUNT_ID": "101-1"})
    assert oanda_credentials(Settings(mode=Mode.PAPER), secrets).base_url == "https://api-fxpractice.oanda.com"


def test_example_config_loads(monkeypatch):
    from pathlib import Path

    for k in ("OANDA_PRACTICE_TOKEN", "OANDA_LIVE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    settings, _ = load(Path(__file__).parents[1] / "config.example.yaml", None)
    assert settings.mode is Mode.RECORD and settings.risk.risk_per_trade_pct == 0.5
    assert settings.logging.json_logs is True


def test_secrets_from_env_types():
    s = Secrets.from_env({"TG_API_ID": "123", "NOTIFY_CHAT_ID": "-55"})
    assert s.tg_api_id == 123 and s.notify_chat_id == -55
