import httpx

from listwatch import cli
from listwatch.fetcher import make_fetcher
from listwatch.probe import FetchResult


def test_fetcher_sends_swedish_locale_and_browser_user_agent():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, text="<html></html>")

    fetch = make_fetcher(transport=httpx.MockTransport(handler))
    res = fetch("https://www.amazon.se/x")
    assert res.status == 200 and res.html == "<html></html>"
    assert seen["accept-language"].startswith("sv-SE")
    assert "Mozilla/5.0" in seen["user-agent"]


def test_fetcher_follows_redirects():
    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200, text="ok")

    res = make_fetcher(transport=httpx.MockTransport(handler))("https://www.amazon.se/a")
    assert res.html == "ok"


def test_cli_probe_prints_report_and_returns_exit_code(capsys, tmp_path, monkeypatch):
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures" / "synthetic_wishlist_p2.html").read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "make_fetcher", lambda: lambda url: FetchResult(200, html, 0.5))
    code = cli.main(["probe", "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Produkter:    1" in out and "avslutskod 0" in out


def test_cli_probe_rejects_non_amazon_se_urls(capsys, tmp_path):
    code = cli.main(["probe", "https://example.com/list", "--out", str(tmp_path)])
    assert code == 2
    assert "amazon.se" in capsys.readouterr().err
