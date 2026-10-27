"""amazon.se as a Source: paginated wishlist pages and product pages.

Load is spread out on top of `fetcher.polite` (5–15 s between any two requests):
- LIST_GAP between lists, after the first.
- Product pages (single-product watches) in groups of PRODUCT_GROUP with PRODUCT_GROUP_GAP
  before each group, so they are not one burst.
- A list read refused with CAPTCHA or HTTP 503 is retried once per omgång after RETRY_WAIT;
  only if that fails too does the omgång count as blocked.
- With an empty cookie session the start page is fetched before the first list (warm-up). A refused
  warm-up counts as a refused list read. After the omgång `finish()` saves the cookies, or clears
  them if the omgång was blocked (spec-fetcher-tls.md).
"""
import random
import re
import time
from typing import Callable, Protocol

from .fetcher import Fetch
from .parser import BASE_URL, is_captcha, parse_product_page, parse_wishlist_page
from .source import ListRead, ProductRead

MAX_PAGES = 50
LIST_GAP = (60, 240)  # seconds
PRODUCT_GROUP = (1, 3)
PRODUCT_GROUP_GAP = (120, 480)
RETRY_WAIT = (300, 900)
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
        read.unparsed += page.unparsed
        next_url = page.next_url
    return read


class CookieSession(Protocol):
    has_cookies: bool

    def save_cookies(self) -> None: ...

    def clear_cookies(self) -> None: ...


def _blocked(read: ListRead) -> str | None:
    if read.captcha:
        return "CAPTCHA"
    return "HTTP 503" if read.http_status == 503 else None


class AmazonSource:
    """One instance per omgång (it keeps the pacing state)."""

    def __init__(
        self,
        fetch: Fetch,
        sleep: Callable[[float], None] = time.sleep,
        on_wait: Callable[[str], None] | None = None,
        session: CookieSession | None = None,
    ):
        self.fetch = fetch
        self.session = session
        self._warm = session is None or session.has_cookies
        self.sleep = sleep
        self.on_wait = on_wait or (lambda _msg: None)
        self._lists_read = 0
        self._retried = False
        self._group_left = 0

    def _wait(self, span: tuple[int, int], why: str) -> None:
        seconds = random.uniform(*span)
        self.on_wait(f"  väntar {seconds / 60:.0f} min {why}")
        self.sleep(seconds)

    def read_list(self, url: str) -> ListRead:
        if self._lists_read:
            self._wait(LIST_GAP, "före nästa lista")
        self._lists_read += 1
        self._group_left = 0  # product pages after this list start a new group
        read = self._read(url)
        reason = _blocked(read)
        if reason and not self._retried:
            self._retried = True
            self._wait(RETRY_WAIT, f"och försöker igen ({reason})")
            read = self._read(url)
            read.retried_after = reason
        return read

    def _read(self, url: str) -> ListRead:
        if not self._warm:
            res = self.fetch(f"{BASE_URL}/")
            if res.status != 200 or is_captcha(res.html):
                return ListRead(http_status=res.status, captcha=res.status == 200, seconds=[res.seconds])
            self._warm = True
        return read_wishlist(url, self.fetch)

    def finish(self, blocked: bool) -> None:
        """Keep the cookies for the next omgång, or drop a session Amazon has refused."""
        if self.session is None:
            return
        if blocked:
            self.session.clear_cookies()
        else:
            self.session.save_cookies()

    def read_product(self, asin: str) -> ProductRead:
        if self._group_left == 0:
            self._wait(PRODUCT_GROUP_GAP, "före nästa produktsidor")
            self._group_left = random.randint(*PRODUCT_GROUP)
        self._group_left -= 1
        res = self.fetch(f"{BASE_URL}/dp/{asin}")
        if res.status != 200:
            return ProductRead(http_status=res.status)
        if is_captcha(res.html):
            return ProductRead(http_status=res.status, captcha=True)
        return ProductRead(http_status=res.status, page=parse_product_page(res.html))
