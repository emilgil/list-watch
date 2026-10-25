from pathlib import Path

from listwatch.parser import is_captcha, parse_wishlist_page

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_extracts_items_from_a_wishlist_page():
    page = parse_wishlist_page(load("synthetic_wishlist_p1.html"))
    assert page.title == "Önskelista teknik"
    assert [i.asin for i in page.items] == [
        "B09XS7JWHH", "B0C1234567", "B08XYZ9876", "B07OUTSTCK",
    ]
    sony = page.items[0]
    assert sony.title == "Sony WH-1000XM5"
    assert sony.price_ore == 299000
    assert sony.image_url == "https://m.media-amazon.com/images/I/sony.jpg"


def test_prime_is_true_when_badge_present_and_none_when_page_does_not_say():
    items = parse_wishlist_page(load("synthetic_wishlist_p1.html")).items
    assert items[0].prime is True
    # Priced item without a Prime badge: the list does not tell us -> unknown
    assert items[2].prime is None


def test_item_without_price_is_missing_and_out_of_stock_is_flagged():
    items = parse_wishlist_page(load("synthetic_wishlist_p1.html")).items
    assert items[1].price_ore is None and items[1].in_stock is True
    assert items[3].price_ore is None and items[3].in_stock is False


def test_next_page_url_is_absolute_and_absent_on_last_page():
    p1 = parse_wishlist_page(load("synthetic_wishlist_p1.html"))
    assert p1.next_url == "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ?lek=PAGE2KEY&page=2"
    p2 = parse_wishlist_page(load("synthetic_wishlist_p2.html"))
    assert p2.next_url is None
    assert p2.items[0].price_ore == 1099500


def test_detects_captcha_page():
    assert is_captcha(load("synthetic_captcha.html"))
    assert not is_captcha(load("synthetic_wishlist_p1.html"))
