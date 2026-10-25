import json
from pathlib import Path

from listwatch.probe import FetchResult, render_report, run_probe

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ"
P2_URL = "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ?lek=PAGE2KEY&page=2"


def html(name):
    return (FIX / name).read_text(encoding="utf-8")


def fake_fetch(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        status, body = pages[url]
        return FetchResult(status=status, html=body, seconds=1.0)

    fetch.calls = calls
    return fetch


def probe(pages, tmp_path):
    return run_probe(URL, fake_fetch(pages), out_dir=tmp_path, sleep=lambda s: None)


GOOD = {
    URL: (200, html("synthetic_wishlist_p1.html")),
    P2_URL: (200, html("synthetic_wishlist_p2.html")),
}


def test_follows_pagination_and_counts_products(tmp_path):
    r = probe(GOOD, tmp_path)
    assert r.pages == 2
    assert r.products == 5
    assert r.list_id == "ABC123XYZ"
    assert r.list_title == "Önskelista teknik"


def test_counts_price_prime_stock_and_product_pages_needed(tmp_path):
    r = probe(GOOD, tmp_path)
    assert r.price_read == 3  # sony, usb, dyr
    assert r.prime_read == 2  # sony, dyr
    assert r.out_of_stock == 1
    assert r.missing_price == ["B0C1234567"]
    # needs product page: missing price (B0C1234567) + priced w/o Prime info (B08XYZ9876)
    assert r.product_pages_needed == 2


def test_partial_result_gives_exit_code_1(tmp_path):
    assert probe(GOOD, tmp_path).exit_code == 1


def test_all_readable_gives_exit_code_0(tmp_path):
    pages = {URL: (200, html("synthetic_wishlist_p2.html"))}
    r = probe(pages, tmp_path)
    assert (r.products, r.exit_code) == (1, 0)


def test_captcha_gives_exit_code_2_and_stops(tmp_path):
    r = probe({URL: (200, html("synthetic_captcha.html"))}, tmp_path)
    assert r.captcha is True and r.exit_code == 2 and r.products == 0


def test_http_503_gives_exit_code_2(tmp_path):
    r = probe({URL: (503, "")}, tmp_path)
    assert r.http_status == 503 and r.exit_code == 2


def test_unparseable_page_gives_exit_code_2(tmp_path):
    r = probe({URL: (200, "<html><body>hej</body></html>")}, tmp_path)
    assert r.products == 0 and r.exit_code == 2


def test_waits_between_requests(tmp_path):
    slept = []
    run_probe(URL, fake_fetch(GOOD), out_dir=tmp_path, sleep=slept.append)
    assert len(slept) == 1 and 5 <= slept[0] <= 15


def test_saves_html_pages_and_report_json(tmp_path):
    r = probe(GOOD, tmp_path)
    saved = Path(r.saved_dir)
    assert saved.parent == tmp_path
    assert (saved / "page-1.html").read_text(encoding="utf-8") == GOOD[URL][1]
    assert (saved / "page-2.html").exists()
    data = json.loads((saved / "report.json").read_text(encoding="utf-8"))
    assert data["products"] == 5 and data["exit_code"] == 1
    assert data["items"][0] == {
        "asin": "B09XS7JWHH", "title": "Sony WH-1000XM5", "price_ore": 299000, "prime": True,
    }


def test_report_text_matches_spec_layout(tmp_path):
    text = render_report(probe(GOOD, tmp_path))
    assert "Produkter:    5" in text
    assert "Pris läst:        3 / 5" in text
    assert "B0C1234567" in text
    assert "Produktsidor behövs per runda:  ~2" in text
    assert "DELVIS OK (avslutskod 1)" in text
