import random
import time
from dataclasses import dataclass
from typing import Callable

import httpx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)
DELAY_RANGE = (5, 15)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Sec-CH-UA": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Linux"',
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


@dataclass
class FetchResult:
    status: int  # 0 = no response
    html: str
    seconds: float


Fetch = Callable[[str], FetchResult]


def polite(fetch: Fetch, sleep: Callable[[float], None] = time.sleep) -> Fetch:
    """Wrap `fetch` so consecutive requests are 5–15 s apart (random), list and product pages alike."""
    first = True

    def wrapped(url: str) -> FetchResult:
        nonlocal first
        if not first:
            sleep(random.uniform(*DELAY_RANGE))
        first = False
        return fetch(url)

    return wrapped


def make_fetcher(transport: httpx.BaseTransport | None = None):
    client = httpx.Client(
        headers=HEADERS, follow_redirects=True, timeout=30, transport=transport
    )

    def fetch(url: str) -> FetchResult:
        t0 = time.monotonic()
        try:
            r = client.get(url)
        except httpx.HTTPError:
            return FetchResult(status=0, html="", seconds=time.monotonic() - t0)
        return FetchResult(status=r.status_code, html=r.text, seconds=time.monotonic() - t0)

    return fetch
