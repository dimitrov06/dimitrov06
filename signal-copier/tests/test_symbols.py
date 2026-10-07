import pytest

from signal_copier.parser import ParserRules, SymbolMapper


@pytest.fixture(scope="module")
def mapper():
    rules = ParserRules.load()
    return SymbolMapper(rules.raw.symbols, rules.raw.fx_currencies)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("XAUUSD", "XAU_USD"),
        ("GOLD", "XAU_USD"),
        ("xau/usd", "XAU_USD"),
        ("US30", "US30_USD"),
        ("DOW", "US30_USD"),
        ("NAS100", "NAS100_USD"),
        ("GER40", "DE30_EUR"),
        ("EURUSD", "EUR_USD"),
        ("GBPJPY", "GBP_JPY"),
        ("USOIL", "WTICO_USD"),
    ],
)
def test_known_symbols(mapper, raw, expected):
    assert mapper.normalize(raw) == expected


@pytest.mark.parametrize("raw", ["SIGNAL", "PEPEUSDT", "EUREUR", "TARGET", "XAUUSDT"])
def test_unknown_symbols(mapper, raw):
    assert mapper.normalize(raw) is None


def test_find_returns_first_symbol_in_text(mapper):
    m = mapper.find("NEW TRADE ON GOLD, LATER EURUSD")
    assert m.instrument == "XAU_USD" and m.raw == "GOLD"
