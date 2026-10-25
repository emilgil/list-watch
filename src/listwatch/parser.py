"""Amazon.se HTML parsing. Isolated so it can be fixed fast when the layout changes."""
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

from .prices import parse_price_ore

BASE_URL = "https://www.amazon.se"
_ASIN_IN_PARAMS = re.compile(r"ASIN:([A-Z0-9]{10})")
_ASIN_IN_HREF = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
_OUT_OF_STOCK = (
    "ej i lager",
    "inte i lager",
    "slutsåld",
    "inte tillgänglig för närvarande",
    "för närvarande inte tillgänglig",
    "för tillfället inte tillgänglig",
    "currently unavailable",
)
# Hidden templates ("Ett fel inträffade…", "Amazon har stött på ett fel") sit next to every item.
_HIDDEN = ".aok-hidden, .a-hidden"
_PRIME_BADGE = "i.a-icon-prime"

STATUS_PRICE = "pris"
STATUS_OUT_OF_STOCK = "ej_i_lager"
STATUS_MISSING_PRICE = "saknar_pris"


@dataclass
class WishlistItem:
    asin: str
    title: str
    image_url: str | None
    price_ore: int | None
    prime: bool | None  # None = the list page does not say (the normal case on amazon.se)
    status: str  # STATUS_PRICE | STATUS_OUT_OF_STOCK | STATUS_MISSING_PRICE


@dataclass
class WishlistPage:
    title: str | None
    items: list[WishlistItem] = field(default_factory=list)
    next_url: str | None = None


@dataclass
class ProductPage:
    price_ore: int | None
    status: str
    prime: bool | None  # True = Prime badge or shipped by Amazon, False = other shipper, None = can't tell
    ships_from_amazon: bool | None  # None = no shipper info on the page
    title: str | None = None


def is_captcha(html: str) -> bool:
    low = html.lower()
    return "validatecaptcha" in low or "/errors/validatecaptcha" in low


def _drop_hidden(tree: HTMLParser) -> None:
    for node in tree.css(_HIDDEN):
        node.decompose()


def _is_out_of_stock(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in _OUT_OF_STOCK)


def _status(price: int | None, out_of_stock: bool) -> str:
    if out_of_stock:
        return STATUS_OUT_OF_STOCK
    return STATUS_PRICE if price is not None else STATUS_MISSING_PRICE


def _first_price(root: Node | HTMLParser, selectors: tuple[str, ...]) -> int | None:
    for sel in selectors:
        node = root.css_first(sel)
        if node is not None:
            price = parse_price_ore(node.text())
            if price is not None:
                return price
    return None


def _asin(li: Node, link: Node | None) -> str | None:
    params = li.attributes.get("data-reposition-action-params") or ""
    m = _ASIN_IN_PARAMS.search(params)
    if m:
        return m.group(1)
    href = (link.attributes.get("href") if link else None) or ""
    m = _ASIN_IN_HREF.search(href)
    return m.group(1) if m else None


def _item(li: Node) -> WishlistItem | None:
    link = li.css_first('a[id^="itemName_"]')
    asin = _asin(li, link)
    if not asin:
        return None
    title = (link.attributes.get("title") or link.text()).strip() if link else ""
    img = li.css_first("img")
    price = _first_price(li, ("span.a-price span.a-offscreen",))
    out_of_stock = price is None and _is_out_of_stock(li.text())
    return WishlistItem(
        asin=asin,
        title=title,
        image_url=img.attributes.get("src") if img else None,
        price_ore=price,
        prime=True if li.css_first(_PRIME_BADGE) is not None else None,
        status=_status(price, out_of_stock),
    )


def parse_wishlist_page(html: str) -> WishlistPage:
    tree = HTMLParser(html)
    _drop_hidden(tree)
    name = tree.css_first("#profile-list-name")
    title_node = name or tree.css_first("title")
    page = WishlistPage(title=title_node.text().strip() if title_node else None)
    for li in tree.css("li[data-itemid]"):
        item = _item(li)
        if item:
            page.items.append(item)
    more = tree.css_first("a.wl-see-more")
    if more is not None and more.attributes.get("href"):
        page.next_url = urljoin(BASE_URL, more.attributes["href"])
    return page


def _ships_from_amazon(tree: HTMLParser) -> bool | None:
    """Read the shipper from the buy box offer details.

    Labelled 'Avsändare' (or combined 'Avsändare / Säljare') or, when Amazon ships for a
    third-party seller, 'Skickas från' next to a separate 'Säljs av'.
    """
    for label in tree.css(".offer-display-feature-label[offer-display-feature-name]"):
        text = label.text().lower()
        if not any(k in text for k in ("avsändare", "skickas från", "ships from")):
            continue
        name = label.attributes["offer-display-feature-name"]
        value = tree.css_first(f'.offer-display-feature-text[offer-display-feature-name="{name}"]')
        if value is not None:
            return value.text().strip().lower().startswith("amazon")
    return None


def parse_product_page(html: str) -> ProductPage:
    tree = HTMLParser(html)
    for node in tree.css("script, style"):
        node.decompose()
    _drop_hidden(tree)
    price = _first_price(
        tree,
        (
            "#corePrice_feature_div span.a-price span.a-offscreen",
            "#corePriceDisplay_desktop_feature_div span.a-price span.a-offscreen",
        ),
    )
    availability = " ".join(
        n.text() for n in tree.css("#availability, #outOfStock")
    )
    out_of_stock = tree.css_first("#outOfStock") is not None or _is_out_of_stock(availability)
    ships_from_amazon = _ships_from_amazon(tree)
    badge = tree.css_first(
        f"#corePrice_feature_div {_PRIME_BADGE}, #apex_desktop {_PRIME_BADGE}, "
        f"#buybox {_PRIME_BADGE}, #deliveryBlockMessage {_PRIME_BADGE}"
    )
    # Logged out, amazon.se shows no Prime badge, so "Avsändare: Amazon" counts as Prime.
    if badge is not None or ships_from_amazon:
        prime = True
    else:
        prime = ships_from_amazon
    title_node = tree.css_first("#productTitle")
    return ProductPage(
        price_ore=None if out_of_stock else price,
        status=_status(price, out_of_stock),
        prime=prime,
        ships_from_amazon=ships_from_amazon,
        title=" ".join(title_node.text().split()) or None if title_node else None,
    )
