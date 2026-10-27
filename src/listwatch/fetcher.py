"""HTTP for amazon.se (spec-fetcher-tls.md).

curl_cffi impersonates Chrome: TLS fingerprint, HTTP/2 and browser headers come from the locked
curl_cffi version. Don't add hand-written browser headers (User-Agent, Sec-*, Accept): they would
contradict the fingerprint. Only Accept-Language is set.

Cookies can persist between omgångar in a JSON file (`.amazon.se` cookies only, mode 0600).
"""
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from http.cookiejar import Cookie
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from curl_cffi import requests

IMPERSONATE = "chrome"
ACCEPT_LANGUAGE = "sv-SE,sv;q=0.9,en;q=0.5"
TIMEOUT = 30
DELAY_RANGE = (5, 15)
COOKIE_DOMAIN = "amazon.se"
COOKIE_FILE = "cookies.json"
_HTTP_VERSIONS = {1: "1.0", 2: "1.1", 3: "2", 30: "3"}  # CURLINFO_HTTP_VERSION

log = logging.getLogger(__name__)


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


def _for_log(url: str) -> str:
    """The URL without the long pagination token (it makes a journal line ~800 chars)."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if "paginationToken" not in query:
        return url
    lid = query.get("lid", [""])[0]
    return f"{parts.scheme}://{parts.netloc}{parts.path}?lid={lid} (nästa sida)"


def make_session() -> requests.Session:
    return requests.Session(impersonate=IMPERSONATE, headers={"Accept-Language": ACCEPT_LANGUAGE})


def _ours(cookie: Cookie) -> bool:
    return cookie.domain.lstrip(".").endswith(COOKIE_DOMAIN)


def _to_dict(c: Cookie) -> dict:
    return {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
            "secure": c.secure, "expires": c.expires}


def _from_dict(d: dict) -> Cookie:
    domain = d["domain"]
    return Cookie(
        version=0, name=d["name"], value=d["value"], port=None, port_specified=False,
        domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
        path=d.get("path") or "/", path_specified=True, secure=bool(d.get("secure")),
        expires=d.get("expires"), discard=d.get("expires") is None, comment=None, comment_url=None, rest={},
    )


class Fetcher:
    """A Fetch over one curl_cffi session. With `cookie_file`, cookies are loaded now and kept by
    `save_cookies()`/`clear_cookies()`; without it (probe) nothing is ever written."""

    def __init__(self, session=None, cookie_file: Path | str | None = None):
        self.session = session if session is not None else make_session()
        self.cookie_file = Path(cookie_file) if cookie_file else None
        if self.cookie_file:
            self._load()

    def __call__(self, url: str) -> FetchResult:
        t0 = time.monotonic()
        try:
            r = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestsError as e:
            seconds = time.monotonic() - t0
            log.info("GET %s → fel: %s (%.1f s)", _for_log(url), e, seconds)
            return FetchResult(status=0, html="", seconds=seconds)
        seconds = time.monotonic() - t0
        version = _HTTP_VERSIONS.get(getattr(r, "http_version", 0), "?")
        log.info("GET %s → %d (HTTP/%s, %.1f s)", _for_log(url), r.status_code, version, seconds)
        return FetchResult(status=r.status_code, html=r.text, seconds=seconds)

    def _load(self) -> None:
        if not self.cookie_file.exists():
            return
        try:
            rows = json.loads(self.cookie_file.read_text(encoding="utf-8"))
            cookies = [_from_dict(d) for d in rows]
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning("cookie-filen %s går inte att läsa (%s), börjar med tom session", self.cookie_file, e)
            return
        for c in cookies:
            if _ours(c) and not c.is_expired():
                self.session.cookies.jar.set_cookie(c)

    def save_cookies(self) -> None:
        if self.cookie_file:
            self._write([_to_dict(c) for c in self.session.cookies.jar if _ours(c)])

    def clear_cookies(self) -> None:
        self.session.cookies.clear()
        if self.cookie_file:
            self._write([])

    def _write(self, rows: list[dict]) -> None:
        tmp = self.cookie_file.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rows, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cookie_file)
        except OSError as e:
            log.warning("kunde inte spara cookies i %s: %s", self.cookie_file, e)


def make_fetcher(session=None, cookie_file: Path | str | None = None) -> Fetcher:
    return Fetcher(session, cookie_file)
