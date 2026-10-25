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
        ("115,66 kr", 11566),
        ("1\u202f299,00\u00a0kr", 129900),  # narrow NBSP thousands separator
        ("10 995,00 kr", 1099500),
        ("1299,00 kr", 129900),
        ("kr217,90", 21790),
        ("115,66kr", 11566),  # product page, logged out
        ("kr1,299.00", 129900),
        ("kr115.66", 11566),
        ("kr299", 29900),
        ("1,299", 129900),
        ("1.299", 129900),
    ],
)
def test_parses_price_formats_to_integer_ore(text, ore):
    assert parse_price_ore(text) == ore


@pytest.mark.parametrize(
    "text, ore",
    [
        ("217,90 kr kr217,90", 21790),
        ("115,66\u00a0kr115,66kr", 11566),
        ("kr1,299.00 1 299,00 kr", 129900),
    ],
)
def test_repeated_price_on_one_row_is_read_once_not_summed(text, ore):
    assert parse_price_ore(text) == ore


@pytest.mark.parametrize("text", ["", "Se alternativ", "Ej i lager", None])
def test_returns_none_when_no_price(text):
    assert parse_price_ore(text) is None
