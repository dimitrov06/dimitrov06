import pytest

from signal_copier.settings import Broker, ConfigError, Mode, Secrets, Settings, load, mt5_credentials, oanda_credentials


def test_default_mode_is_record(tmp_path):
    settings, _ = load(tmp_path / "missing.yaml", None)
    assert settings.mode is Mode.RECORD
    assert oanda_credentials(settings, Secrets()) is None


def test_live_requires_explicit_confirm():
    s = Settings(mode=Mode.LIVE, broker=Broker.OANDA)
    secrets = Secrets.from_env({"OANDA_LIVE_TOKEN": "x", "OANDA_LIVE_ACCOUNT_ID": "1", "LIVE_CONFIRM": "no"})
    with pytest.raises(ConfigError, match="LIVE_CONFIRM"):
        oanda_credentials(s, secrets)
    secrets = Secrets.from_env({"OANDA_LIVE_TOKEN": "x", "OANDA_LIVE_ACCOUNT_ID": "1", "LIVE_CONFIRM": "YES"})
    assert oanda_credentials(s, secrets).base_url == "https://api-fxtrade.oanda.com"


def test_paper_uses_practice_and_needs_token():
    with pytest.raises(ConfigError):
        oanda_credentials(Settings(mode=Mode.PAPER, broker=Broker.OANDA), Secrets())
    secrets = Secrets.from_env({"OANDA_PRACTICE_TOKEN": "x", "OANDA_PRACTICE_ACCOUNT_ID": "101-1"})
    assert oanda_credentials(Settings(mode=Mode.PAPER, broker=Broker.OANDA), secrets).base_url == "https://api-fxpractice.oanda.com"


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


MT5_ENV = {"MT5_LOGIN": "5100123", "MT5_PASSWORD": "pw", "MT5_SERVER": "ICMarketsSC-Demo"}


def test_mt5_is_default_broker_and_paper_expects_demo():
    s = Settings(mode=Mode.PAPER)
    assert s.broker is Broker.MT5
    creds = mt5_credentials(s, Secrets.from_env(MT5_ENV))
    assert creds.login == 5100123 and creds.expect_demo is True
    assert oanda_credentials(s, Secrets.from_env(MT5_ENV)) is None


def test_mt5_live_needs_confirm_and_real_account():
    s = Settings(mode=Mode.LIVE)
    with pytest.raises(ConfigError, match="LIVE_CONFIRM"):
        mt5_credentials(s, Secrets.from_env(MT5_ENV))
    creds = mt5_credentials(s, Secrets.from_env({**MT5_ENV, "LIVE_CONFIRM": "yes"}))
    assert creds.expect_demo is False


def test_mt5_partial_login_is_an_error():
    with pytest.raises(ConfigError, match="MT5_LOGIN"):
        mt5_credentials(Settings(mode=Mode.PAPER), Secrets.from_env({"MT5_LOGIN": "5100123"}))


def test_mt5_without_login_uses_logged_in_terminal():
    creds = mt5_credentials(Settings(mode=Mode.PAPER), Secrets.from_env({"MT5_LOGIN": "", "MT5_PASSWORD": ""}))
    assert creds.login is None and creds.expect_demo is True
    from signal_copier.settings import mt5_check_credentials
    assert mt5_check_credentials(Settings(mode=Mode.RECORD), Secrets()).expect_demo is None
