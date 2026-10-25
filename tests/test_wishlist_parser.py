from pathlib import Path

import pytest

from listwatch.parser import (
    STATUS_MISSING_PRICE,
    STATUS_OUT_OF_STOCK,
    STATUS_PRICE,
    is_captcha,
    parse_wishlist_page,
)

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
    assert items[0].status == STATUS_PRICE
    assert items[1].price_ore is None and items[1].status == STATUS_MISSING_PRICE
    assert items[3].price_ore is None and items[3].status == STATUS_OUT_OF_STOCK


def _li(body):
    return (
        '<ul><li data-itemid="I1" data-reposition-action-params=\'{"itemExternalId":"ASIN:B000000001|A1"}\'>'
        f'<a id="itemName_I1" title="T" href="/dp/B000000001/">T</a>{body}</li></ul>'
    )


@pytest.mark.parametrize(
    "marker",
    ["Inte tillgänglig för närvarande.", "För närvarande inte tillgänglig.", "Currently unavailable."],
)
def test_visible_unavailable_marker_gives_out_of_stock(marker):
    item = parse_wishlist_page(_li(f"<span>{marker}</span>")).items[0]
    assert item.status == STATUS_OUT_OF_STOCK


def test_hidden_templates_are_ignored():
    hidden = (
        '<div class="a-box a-alert a-hidden"><h4>Ett fel inträffade, försök igen om en stund</h4></div>'
        '<div class="aok-hidden"><span>Currently unavailable</span></div>'
    )
    item = parse_wishlist_page(_li(hidden + "<span>Visa alla köpalternativ</span>")).items[0]
    assert item.status == STATUS_MISSING_PRICE


def test_real_list_page_prices_and_missing_prices():
    page = parse_wishlist_page(load("real_wishlist_items.html"))
    assert page.title == "Hemmet"
    by_asin = {i.asin: i for i in page.items}
    assert list(by_asin) == ["B07V5J9RNV", "B003YT2HLE", "B08WRMRF44"]
    assert by_asin["B07V5J9RNV"].price_ore == 21790
    assert by_asin["B07V5J9RNV"].status == STATUS_PRICE
    # "Visa alla köpalternativ" only: no price, no unavailable marker on the list page
    assert by_asin["B003YT2HLE"].status == STATUS_MISSING_PRICE
    assert by_asin["B08WRMRF44"].status == STATUS_MISSING_PRICE
    # The real list page never shows Prime
    assert all(i.prime is None for i in page.items)
    assert page.next_url is None


def test_next_page_url_is_absolute_and_absent_on_last_page():
    p1 = parse_wishlist_page(load("synthetic_wishlist_p1.html"))
    assert p1.next_url == "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ?lek=PAGE2KEY&page=2"
    p2 = parse_wishlist_page(load("synthetic_wishlist_p2.html"))
    assert p2.next_url is None
    assert p2.items[0].price_ore == 1099500


def test_detects_captcha_page():
    assert is_captcha(load("synthetic_captcha.html"))
    assert not is_captcha(load("synthetic_wishlist_p1.html"))
