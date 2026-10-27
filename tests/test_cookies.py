import json
import logging
from pathlib import Path
from types import SimpleNamespace

from curl_cffi.requests import Cookies

from listwatch.amazon import AmazonSource
from listwatch.fetcher import Fetcher, FetchResult, _from_dict
from listwatch.db import setting_int
from listwatch.scheduler import run_omgang
from test_collector import Clock, add_list, conn  # noqa: F401  (fixture)
from test_scheduler import FakeNotifier

FIX = Path(__file__).parent / "fixtures"
LIST_URL = "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ"
HOME = "https://www.amazon.se/"


def amazon_cookie(name="session-id", value="1", domain=".amazon.se"):
    return _from_dict({"name": name, "value": value, "domain": domain, "path": "/", "expires": None})


class FakeSession:
    """Stands in for a curl_cffi Session; never touches the network.

    `responses` maps URL → (status, html); others get 200. A 200 sets an amazon.se cookie,
    like the real site does.
    """

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.cookies = Cookies()
        self.urls: list[str] = []
        self.kwargs: list[dict] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        self.kwargs.append(kwargs)
        if self.error:
            raise self.error
        status, html = self.responses.get(url, (200, "<html></html>"))
        if status == 200:
            self.cookies.jar.set_cookie(amazon_cookie())
        return SimpleNamespace(status_code=status, text=html, http_version=3)


def saved(path):
    return {(c["name"], c["value"], c["domain"]) for c in json.loads(path.read_text())}


def test_cookies_are_saved_and_loaded_again(tmp_path):
    path = tmp_path / "cookies.json"
    session = FakeSession()
    session.cookies.jar.set_cookie(amazon_cookie("ubid-acbse", "x"))
    session.cookies.jar.set_cookie(amazon_cookie("other", "y", domain=".example.com"))
    Fetcher(session, path).save_cookies()
    assert saved(path) == {("ubid-acbse", "x", ".amazon.se")}
    assert path.stat().st_mode & 0o777 == 0o600
    again = Fetcher(FakeSession(), path)
    assert {(c.name, c.value) for c in again.session.cookies.jar} == {("ubid-acbse", "x")}


def test_broken_cookie_file_gives_an_empty_session_and_a_warning(tmp_path, caplog):
    path = tmp_path / "cookies.json"
    path.write_text("{trasig")
    f = Fetcher(FakeSession(), path)
    assert list(f.session.cookies.jar) == []
    assert "går inte att läsa" in caplog.text
    f.save_cookies()
    assert saved(path) == set()


def test_without_cookie_file_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = Fetcher(FakeSession())
    f("https://www.amazon.se/x")
    f.save_cookies()
    f.clear_cookies()
    assert list(tmp_path.iterdir()) == []


def source(session, path):
    fetcher = Fetcher(session, path)
    return AmazonSource(fetcher, sleep=lambda s: None, session=fetcher)


def wishlist():
    return (FIX / "real_wishlist_items.html").read_text(encoding="utf-8")


def test_no_start_page_request_even_with_an_empty_session(tmp_path):
    session = FakeSession({LIST_URL: (200, wishlist())})
    source(session, tmp_path / "cookies.json").read_list(LIST_URL)
    assert session.urls[0] == LIST_URL
    assert HOME not in session.urls


def test_http_202_on_a_list_is_retried(tmp_path):
    session = FakeSession({LIST_URL: (202, "")})
    read = source(session, tmp_path / "cookies.json").read_list(LIST_URL)
    assert session.urls == [LIST_URL, LIST_URL]
    assert read.retried_after == "HTTP 202 (JS-utmaning)" and read.blocked


def test_successful_omgang_saves_cookies(conn, tmp_path):
    add_list(conn, LIST_URL)
    path = tmp_path / "cookies.json"
    run_omgang(conn, source(FakeSession({LIST_URL: (200, wishlist())}), path), now=Clock(), notifier=FakeNotifier())
    assert saved(path) == {("session-id", "1", ".amazon.se")}


def test_blocked_omgang_clears_the_cookie_file(conn, tmp_path):
    add_list(conn, LIST_URL)
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps([{"name": "session-id", "value": "1", "domain": ".amazon.se", "path": "/"}]))
    res = run_omgang(conn, source(FakeSession({LIST_URL: (503, "")}), path), now=Clock(), notifier=FakeNotifier())
    assert res.blocked
    assert saved(path) == set()


def test_http_202_twice_blocks_the_omgang_clears_cookies_and_backs_off(conn, tmp_path):
    add_list(conn, LIST_URL)
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps([{"name": "session-id", "value": "1", "domain": ".amazon.se", "path": "/"}]))
    res = run_omgang(conn, source(FakeSession({LIST_URL: (202, "")}), path), now=Clock(), notifier=FakeNotifier())
    assert res.blocked and setting_int(conn, "blockerade_i_rad") == 1
    assert saved(path) == set()
    texts = [r[0] for r in conn.execute("SELECT text FROM event ORDER BY id")]
    assert "listsida: HTTP 202 (JS-utmaning)" in texts
