import logging

from curl_cffi import requests

from listwatch import cli
from listwatch.fetcher import ACCEPT_LANGUAGE, FetchResult, make_fetcher, make_session, polite
from test_cookies import FakeSession


def test_session_impersonates_chrome_and_only_sets_accept_language():
    s = make_session()
    assert s.impersonate == "chrome"
    assert dict(s.headers) == {"accept-language": ACCEPT_LANGUAGE}


def test_fetcher_passes_status_and_html_through_and_follows_redirects():
    session = FakeSession({"https://www.amazon.se/x": (404, "<html>nej</html>")})
    res = make_fetcher(session)("https://www.amazon.se/x")
    assert (res.status, res.html) == (404, "<html>nej</html>")
    assert session.kwargs[0]["allow_redirects"] is True and session.kwargs[0]["timeout"] == 30
    assert "headers" not in session.kwargs[0]


def test_network_error_gives_status_0():
    res = make_fetcher(FakeSession(error=requests.RequestsError("down")))("https://www.amazon.se/x")
    assert (res.status, res.html) == (0, "")


def test_fetcher_logs_status_and_http_version(caplog):
    caplog.set_level(logging.INFO, logger="listwatch.fetcher")
    make_fetcher(FakeSession())("https://www.amazon.se/x")
    assert "GET https://www.amazon.se/x → 200 (HTTP/2," in caplog.text


def test_polite_waits_5_to_15_s_between_requests_but_not_before_the_first():
    slept = []
    fetch = polite(lambda url: FetchResult(200, url, 0.1), sleep=slept.append)
    for u in ("a", "b", "c"):
        fetch(u)
    assert len(slept) == 2 and all(5 <= s <= 15 for s in slept)


def test_cli_probe_prints_report_and_returns_exit_code(capsys, tmp_path, monkeypatch):
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures" / "synthetic_wishlist_p2.html").read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "make_fetcher", lambda: lambda url: FetchResult(200, html, 0.5))
    code = cli.main(["probe", "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Produkter:    1" in out and "avslutskod 0" in out


def test_cli_probe_uses_a_session_without_cookie_file(tmp_path, monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return lambda url: FetchResult(503, "", 0.1)

    monkeypatch.setattr(cli, "make_fetcher", fake)
    cli.main(["probe", "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ", "--out", str(tmp_path)])
    assert calls == [((), {})]


def test_cli_probe_rejects_non_amazon_se_urls(capsys, tmp_path):
    code = cli.main(["probe", "https://example.com/list", "--out", str(tmp_path)])
    assert code == 2
    assert "amazon.se" in capsys.readouterr().err
