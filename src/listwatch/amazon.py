"""amazon.se as a Source: paginated wishlist pages and product pages."""
import re
from typing import Callable

from .fetcher import Fetch
from .parser import BASE_URL, is_captcha, parse_product_page, parse_wishlist_page
from .source import ListRead, ProductRead

MAX_PAGES = 50
_LIST_ID = re.compile(r"/wishlist/ls/([A-Z0-9]+)")


def list_id_from_url(url: str) -> str | None:
    m = _LIST_ID.search(url)
    return m.group(1) if m else None


def read_wishlist(
    url: str, fetch: Fetch, on_page: Callable[[int, str], None] | None = None
) -> ListRead:
    """Follow the pagination until the last page or the first error."""
    read = ListRead()
    next_url: str | None = url
    while next_url and read.pages < MAX_PAGES:
        res = fetch(next_url)
        read.http_status = res.status
        read.seconds.append(res.seconds)
        if res.status != 200:
            break
        if is_captcha(res.html):
            read.captcha = True
            break
        read.pages += 1
        if on_page:
            on_page(read.pages, res.html)
        page = parse_wishlist_page(res.html)
        read.title = read.title or page.title
        read.items.extend(page.items)
        next_url = page.next_url
    return read


class AmazonSource:
    def __init__(self, fetch: Fetch):
        self.fetch = fetch

    def read_list(self, url: str) -> ListRead:
        return read_wishlist(url, self.fetch)

    def read_product(self, asin: str) -> ProductRead:
        res = self.fetch(f"{BASE_URL}/dp/{asin}")
        if res.status != 200:
            return ProductRead(http_status=res.status)
        if is_captcha(res.html):
            return ProductRead(http_status=res.status, captcha=True)
        return ProductRead(http_status=res.status, page=parse_product_page(res.html))
