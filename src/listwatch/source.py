"""The interface the collector reads through, so other sources can be added later."""
from dataclasses import dataclass, field
from typing import Protocol

from .parser import ProductPage, WishlistItem


@dataclass
class ListRead:
    items: list[WishlistItem] = field(default_factory=list)
    title: str | None = None
    pages: int = 0
    http_status: int | None = None
    captcha: bool = False
    seconds: list[float] = field(default_factory=list)

    @property
    def error(self) -> str | None:
        """'captcha', 'http' or 'parse' (no items found); None if the list was read."""
        if self.captcha:
            return "captcha"
        if self.http_status != 200:
            return "http"
        return None if self.items else "parse"


@dataclass
class ProductRead:
    http_status: int
    captcha: bool = False
    page: ProductPage | None = None


class Source(Protocol):
    def read_list(self, url: str) -> ListRead: ...

    def read_product(self, asin: str) -> ProductRead: ...
