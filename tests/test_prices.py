import pytest

from listwatch.prices import parse_price_ore


@pytest.mark.parametrize(
    "text, ore",
    [
        ("1 299,00 kr", 129900),
        ("1 299,00\xa0kr", 129900),
        ("1 299,50 kr", 129950),
        ("299 kr", 29900),
        ("SEK 2 990,00", 299000),
        ("2.990,00 kr", 299000),
        ("0,99 kr", 99),
        ("49,9 kr", 4990),
    ],
)
def test_parses_swedish_price_formats_to_integer_ore(text, ore):
    assert parse_price_ore(text) == ore


@pytest.mark.parametrize("text", ["", "Se alternativ", "Ej i lager", None])
def test_returns_none_when_no_price(text):
    assert parse_price_ore(text) is None
