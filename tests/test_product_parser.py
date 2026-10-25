from pathlib import Path

from listwatch.parser import STATUS_MISSING_PRICE, STATUS_OUT_OF_STOCK, STATUS_PRICE, parse_product_page

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_real_in_stock_product_sold_by_amazon():
    p = parse_product_page(load("real_product_B0C93G2M83.html"))
    assert p.price_ore == 11566
    assert p.status == STATUS_PRICE
    # Logged out, amazon.se shows no Prime badge; shipped by Amazon counts as Prime
    assert p.ships_from_amazon is True
    assert p.prime is True


def test_real_unavailable_product():
    p = parse_product_page(load("real_product_B07FK9P738.html"))
    assert p.status == STATUS_OUT_OF_STOCK
    assert p.price_ore is None
    assert p.prime is None


def _page(buybox):
    return f'<html><body><div id="buybox">{buybox}</div></body></html>'


def _merchant(label, value):
    return (
        f'<div class="offer-display-feature-label" offer-display-feature-name="desktop-merchant-info">'
        f"<span>{label}</span></div>"
        f'<div class="offer-display-feature-text" offer-display-feature-name="desktop-merchant-info">'
        f'<span class="offer-display-feature-text-message">{value}</span></div>'
    )


PRICE = '<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">kr1,299.00</span></span></div>'


def test_prime_badge_in_buy_box_means_prime():
    p = parse_product_page(_page(PRICE + '<i class="a-icon a-icon-prime"></i>'))
    assert (p.price_ore, p.prime) == (129900, True)


def test_split_shipper_and_seller_uses_shipper():
    buybox = PRICE + _merchant("Avsändare", "Amazon").replace("merchant-info", "fulfiller-info")
    buybox += _merchant("Säljare", "Någon Butik AB")
    assert parse_product_page(_page(buybox)).prime is True


def test_third_party_shipper_means_not_prime():
    p = parse_product_page(_page(PRICE + _merchant("Avsändare / Säljare", "Någon Butik AB")))
    assert (p.ships_from_amazon, p.prime) == (False, False)


def test_english_unavailable_marker():
    html = _page('<div id="availability"><span>Currently unavailable.</span></div>')
    assert parse_product_page(html).status == STATUS_OUT_OF_STOCK


def test_no_price_and_no_marker_is_missing_price():
    assert parse_product_page(_page("<span>Visa alla köpalternativ</span>")).status == STATUS_MISSING_PRICE
