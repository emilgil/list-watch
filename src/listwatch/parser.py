"""Amazon.se HTML parsing. Isolated so it can be fixed fast when the layout changes."""
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

from .prices import parse_price_ore

BASE_URL = "https://www.amazon.se"
_ASIN_IN_PARAMS = re.compile(r"ASIN:([A-Z0-9]{10})")
_ASIN_IN_HREF = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
_OUT_OF_STOCK = ("ej i lager", "inte i lager", "slutsåld", "för tillfället inte tillgänglig")


@dataclass
class WishlistItem:
    asin: str
    title: str
    image_url: str | None
    price_ore: int | None
    prime: bool | None  # None = the list page does not say
    in_stock: bool


@dataclass
class WishlistPage:
    title: str | None
    items: list[WishlistItem] = field(default_factory=list)
    next_url: str | None = None


def is_captcha(html: str) -> bool:
    low = html.lower()
    return "validatecaptcha" in low or "/errors/validatecaptcha" in low


def _asin(li: Node, link: Node | None) -> str | None:
    params = li.attributes.get("data-reposition-action-params") or ""
    m = _ASIN_IN_PARAMS.search(params)
    if m:
        return m.group(1)
    href = (link.attributes.get("href") if link else None) or ""
    m = _ASIN_IN_HREF.search(href)
    return m.group(1) if m else None


def _price(li: Node) -> int | None:
    node = li.css_first("span.a-price span.a-offscreen")
    if node is not None:
        return parse_price_ore(node.text())
    return None


def _item(li: Node) -> WishlistItem | None:
    link = li.css_first('a[id^="itemName_"]')
    asin = _asin(li, link)
    if not asin:
        return None
    title = (link.attributes.get("title") or link.text()).strip() if link else ""
    img = li.css_first("img")
    price = _price(li)
    text = li.text().lower()
    in_stock = not (price is None and any(s in text for s in _OUT_OF_STOCK))
    prime = True if li.css_first("i.a-icon-prime") is not None else None
    return WishlistItem(
        asin=asin,
        title=title,
        image_url=img.attributes.get("src") if img else None,
        price_ore=price,
        prime=prime,
        in_stock=in_stock,
    )


def parse_wishlist_page(html: str) -> WishlistPage:
    tree = HTMLParser(html)
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
