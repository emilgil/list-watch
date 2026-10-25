import time

import httpx

from .probe import FetchResult

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


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
