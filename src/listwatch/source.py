"""The interface the collector reads through, so other sources can be added later."""
from dataclasses import dataclass, field
from typing import Protocol

from .parser import ProductPage, WishlistItem

# Amazon refusing a request: 503, or 202 = JavaScript challenge (AWS WAF), which curl_cffi
# can't pass. Like CAPTCHA these are retried/backed off; other non-200 statuses are errors.
BLOCKED_STATUSES = {202: "HTTP 202 (JS-utmaning)", 503: "HTTP 503"}
BLOCKED_TEXT = "CAPTCHA, HTTP 503 eller HTTP 202"


def blocked_reason(captcha: bool, http_status: int | None) -> str | None:
    """'CAPTCHA', 'HTTP 202 (JS-utmaning)' or 'HTTP 503' if Amazon refused, else None."""
    return "CAPTCHA" if captcha else BLOCKED_STATUSES.get(http_status)


@dataclass
class ListRead:
    items: list[WishlistItem] = field(default_factory=list)
    title: str | None = None
    pages: int = 0
    http_status: int | None = None
    captcha: bool = False
    seconds: list[float] = field(default_factory=list)
    unparsed: int = 0  # list items the parser could not read
    retried_after: str | None = None  # this is a retry; the first read was refused for this reason

    @property
    def error(self) -> str | None:
        """'captcha', 'http' or 'parse' (no items found); None if the list was read."""
        if self.captcha:
            return "captcha"
        if self.http_status != 200:
            return "http"
        return None if self.items else "parse"

    @property
    def blocked(self) -> str | None:
        return blocked_reason(self.captcha, self.http_status)


@dataclass
class ProductRead:
    http_status: int
    captcha: bool = False
    page: ProductPage | None = None

    @property
    def blocked(self) -> str | None:
        return blocked_reason(self.captcha, self.http_status)


class Source(Protocol):
    def read_list(self, url: str) -> ListRead: ...

    def read_product(self, asin: str) -> ProductRead: ...
