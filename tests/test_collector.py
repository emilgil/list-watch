from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from listwatch.amazon import AmazonSource
from listwatch.collector import collect
from listwatch.db import connect, migrate, utc_iso
from listwatch.fetcher import FetchResult
from listwatch.parser import ProductPage, WishlistItem
from listwatch.source import ListRead, ProductRead

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
FIX = Path(__file__).parent / "fixtures"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def item(asin, price=None, status=None, title=None):
    status = status or ("pris" if price is not None else "saknar_pris")
    return WishlistItem(asin=asin, title=title or asin, image_url=None, price_ore=price, prime=None, status=status)


def ok(*items):
    return ListRead(items=list(items), title="L", pages=1, http_status=200)


def page(prime=None, status="pris", price=10000):
    return ProductRead(200, page=ProductPage(price_ore=price, status=status, prime=prime, ships_from_amazon=prime))


class FakeSource:
    def __init__(self):
        self.lists: dict[str, ListRead] = {}
        self.products: dict[str, ProductRead] = {}
        self.product_calls: list[str] = []

    def read_list(self, url):
        return self.lists[url]

    def read_product(self, asin):
        self.product_calls.append(asin)
        return self.products.get(asin, page())


@pytest.fixture
def conn(tmp_path):
    migrate(tmp_path / "lw.db")
    c = connect(tmp_path / "lw.db")
    yield c
    c.close()


def add_list(conn, url="L1", typ="amazon"):
    return conn.execute(
        "INSERT INTO wishlist (typ, namn, url, list_id, skapad) VALUES (?, ?, ?, ?, ?)",
        (typ, url, url, url if typ == "amazon" else None, utc_iso(T0)),
    ).lastrowid


def run_row(conn, run_id):
    return dict(conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone())


def events(conn, run_id=None):
    sql = "SELECT typ, asin, fore, efter FROM event"
    rows = conn.execute(sql + (" WHERE run_id = ?" if run_id else "") + " ORDER BY id", (run_id,) if run_id else ())
    return [tuple(r) for r in rows]


def product(conn, asin):
    return dict(conn.execute("SELECT * FROM product WHERE asin = ?", (asin,)).fetchone())


def test_first_run_imports_products_and_logs_run(conn):
    wl = add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 11566), item("A000000002"))
    [run_id] = collect(conn, src, now=Clock())

    r = run_row(conn, run_id)
    assert (r["omgang"], r["typ"], r["wishlist_id"], r["list_namn"]) == (1, "schemalagd", wl, "L")  # took Amazon's title
    assert (r["sidor"], r["lasta"], r["nya"], r["prisandrade"], r["fel"]) == (1, 2, 2, 0, 0)
    assert r["resultat"] == "delvis"  # one product lacks a price
    assert events(conn, run_id) == [
        ("ny_produkt", "A000000001", None, None),
        ("ny_produkt", "A000000002", None, None),
        ("saknar_pris", "A000000002", None, None),
    ]
    p = product(conn, "A000000001")
    assert (p["senaste_status"], p["senaste_pris_ore"], p["prime_status"], p["aktiv"]) == ("pris", 11566, "okänd", 1)
    assert p["url"] == "https://www.amazon.se/dp/A000000001"
    points = conn.execute("SELECT asin, status, pris_ore, kalla, run_id FROM price_point ORDER BY asin").fetchall()
    assert [tuple(x) for x in points] == [
        ("A000000001", "pris", 11566, "lista", run_id),
        ("A000000002", "saknar_pris", None, "lista", run_id),
    ]
    assert conn.execute("SELECT status, senast_synkad FROM wishlist").fetchone()[0] == "ok"


def test_only_changes_are_logged(conn):
    add_list(conn)
    src, clock = FakeSource(), Clock()
    src.lists["L1"] = ok(item("A000000001", 100), item("A000000002", 200), item("A000000003"))
    collect(conn, src, now=clock)
    clock.advance(hours=6)
    src.lists["L1"] = ok(item("A000000001", 90), item("A000000002", 200), item("A000000003"))
    [run_id] = collect(conn, src, now=clock)

    r = run_row(conn, run_id)
    assert (r["omgang"], r["lasta"], r["nya"], r["prisandrade"]) == (2, 3, 0, 1)
    # unchanged price and a still-missing price are not events
    assert events(conn, run_id) == [("pris_ändrat", "A000000001", "100", "90")]
    assert conn.execute("SELECT count(*) FROM price_point").fetchone()[0] == 6


def test_status_transitions(conn):
    add_list(conn)
    src, clock = FakeSource(), Clock()
    steps = [
        (item("A000000001", 100), []),
        (item("A000000001", status="ej_i_lager"), [("ej_i_lager", "A000000001", "pris", None)]),
        (item("A000000001", 120), [("åter_i_lager", "A000000001", "100", "120")]),
        (item("A000000001"), [("saknar_pris", "A000000001", "pris", None)]),
        (item("A000000001", 120), []),  # back to the last known price: no change
    ]
    for it, expected in steps:
        src.lists["L1"] = ok(it)
        [run_id] = collect(conn, src, now=clock)
        assert [e for e in events(conn, run_id) if e[0] != "ny_produkt"] == expected
        clock.advance(hours=6)


def test_removed_and_readded_items(conn):
    add_list(conn)
    src, clock = FakeSource(), Clock()
    src.lists["L1"] = ok(item("A000000001", 100), item("A000000002", 200))
    collect(conn, src, now=clock)
    src.lists["L1"] = ok(item("A000000001", 100))
    [run_id] = collect(conn, src, now=clock)
    assert run_row(conn, run_id)["borttagna"] == 1
    assert events(conn, run_id) == [("borttagen_produkt", "A000000002", None, None)]
    assert product(conn, "A000000002")["aktiv"] == 0
    # history is kept
    assert conn.execute("SELECT count(*) FROM price_point WHERE asin = 'A000000002'").fetchone()[0] == 1

    src.lists["L1"] = ok(item("A000000001", 100), item("A000000002", 200))
    [run_id] = collect(conn, src, now=clock)
    assert events(conn, run_id) == [("ny_produkt", "A000000002", None, None)]
    assert product(conn, "A000000002")["aktiv"] == 1


def test_product_in_two_lists_is_measured_once_per_omgang(conn):
    add_list(conn, "L1")
    add_list(conn, "L2")
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 100))
    src.lists["L2"] = ok(item("A000000001", 100), item("A000000002", 50))
    r1, r2 = collect(conn, src, now=Clock())
    assert run_row(conn, r1)["omgang"] == run_row(conn, r2)["omgang"]
    assert (run_row(conn, r1)["lasta"], run_row(conn, r2)["lasta"]) == (1, 2)
    assert conn.execute("SELECT count(*) FROM price_point WHERE asin = 'A000000001'").fetchone()[0] == 1
    # membership is per list, so both runs log the new product
    assert ("ny_produkt", "A000000001", None, None) in events(conn, r2)


def test_list_captcha_fails_the_run_without_touching_membership(conn):
    add_list(conn)
    src, clock = FakeSource(), Clock()
    src.lists["L1"] = ok(item("A000000001", 100))
    collect(conn, src, now=clock)
    src.lists["L1"] = ListRead(http_status=200, captcha=True)
    [run_id] = collect(conn, src, now=clock)
    r = run_row(conn, run_id)
    assert (r["resultat"], r["captcha"], r["fel"], r["borttagna"]) == ("fel", "listsida", 1, 0)
    assert events(conn, run_id) == [("captcha", None, None, None)]
    assert conn.execute("SELECT status FROM wishlist").fetchone()[0] == "blockerad"
    assert product(conn, "A000000001")["aktiv"] == 1


@pytest.mark.parametrize(
    "read, event, status",
    [
        (ListRead(http_status=503), "http_fel", "blockerad"),
        (ListRead(http_status=500), "http_fel", "fel"),
        (ListRead(http_status=404), "http_fel", "privat"),
        (ListRead(http_status=200, pages=1), "parsningsfel", "privat"),
    ],
)
def test_list_errors(conn, read, event, status):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = read
    [run_id] = collect(conn, src, now=Clock())
    assert run_row(conn, run_id)["resultat"] == "fel"
    assert [e[0] for e in events(conn, run_id)] == [event]
    assert conn.execute("SELECT status FROM wishlist").fetchone()[0] == status


def test_lists_fetch_no_product_pages(conn):
    # Prime is not tracked (2026-10-27): a list omgång reads only the list pages
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 100), item("A000000002"))
    [run_id] = collect(conn, src, now=Clock())
    assert src.product_calls == [] and run_row(conn, run_id)["produktsidor"] == 0

def local_watch(conn, *asins):
    wl = add_list(conn, "lokal", typ="lokal")
    for asin in asins:
        conn.execute("INSERT INTO product (asin, forsta_sedd) VALUES (?, ?)", (asin, utc_iso(T0)))
        conn.execute("INSERT INTO wishlist_product (wishlist_id, asin, tillagd) VALUES (?, ?, ?)", (wl, asin, utc_iso(T0)))
    return wl


def test_product_page_captcha_stops_product_pages_for_the_omgang(conn):
    local_watch(conn, "A000000001", "A000000002")
    src = FakeSource()
    src.products["A000000001"] = ProductRead(200, captcha=True)
    [run_id] = collect(conn, src, now=Clock())
    assert src.product_calls == ["A000000001"]
    r = run_row(conn, run_id)
    assert (r["captcha"], r["fel"], r["resultat"]) == ("produktsida", 1, "fel")

def test_price_below_10_percent_of_median_is_suspicious(conn):
    add_list(conn)
    src, clock = FakeSource(), Clock()
    for price in (1000, 1000, 1000, 50, 52, 50, 1000, 40, 900):
        src.lists["L1"] = ok(item("A000000001", price))
        collect(conn, src, now=clock)
        clock.advance(hours=6)
    flags = [r[0] for r in conn.execute("SELECT misstankt FROM price_point ORDER BY id")]
    # 50 is suspicious, confirmed by 52 and stays confirmed; a lone 40 is not confirmed
    assert flags == [0, 0, 0, 1, 0, 0, 0, 1, 0]


def test_local_watches_read_product_pages(conn):
    local_watch(conn, "A000000001", "A000000002")
    src = FakeSource()
    src.products["A000000001"] = page(prime=True, price=50000)
    src.products["A000000002"] = page(status="ej_i_lager", price=None)
    [run_id] = collect(conn, src, now=Clock())
    r = run_row(conn, run_id)
    assert (r["lasta"], r["produktsidor"], r["resultat"]) == (2, 2, "ok")
    points = conn.execute("SELECT asin, status, pris_ore, kalla FROM price_point ORDER BY asin").fetchall()
    assert [tuple(p) for p in points] == [
        ("A000000001", "pris", 50000, "produktsida"),
        ("A000000002", "ej_i_lager", None, "produktsida"),
    ]
    assert "produktsida_hämtad" not in [e[0] for e in events(conn, run_id)]
    assert product(conn, "A000000001")["prime_status"] == "okänd"  # not tracked

def test_local_watch_blocked_fails_the_run(conn):
    wl = add_list(conn, "lokal", typ="lokal")
    conn.execute("INSERT INTO product (asin, forsta_sedd) VALUES ('A000000001', ?)", (utc_iso(T0),))
    conn.execute("INSERT INTO wishlist_product (wishlist_id, asin, tillagd) VALUES (?, 'A000000001', ?)", (wl, utc_iso(T0)))
    src = FakeSource()
    src.products["A000000001"] = ProductRead(503)
    [run_id] = collect(conn, src, now=Clock())
    assert run_row(conn, run_id)["resultat"] == "fel"
    assert conn.execute("SELECT status FROM wishlist").fetchone()[0] == "blockerad"


def test_empty_local_list_is_ok(conn):
    add_list(conn, "lokal", typ="lokal")
    [run_id] = collect(conn, FakeSource(), now=Clock())
    assert run_row(conn, run_id)["resultat"] == "ok"


def test_amazon_source_against_real_fixtures(conn):
    pages = {
        "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ": (FIX / "real_wishlist_items.html").read_text(encoding="utf-8"),
    }
    add_list(conn, "https://www.amazon.se/hz/wishlist/ls/ABC123XYZ")
    source = AmazonSource(lambda url: FetchResult(200, pages[url], 0.1), sleep=lambda s: None)
    [run_id] = collect(conn, source, now=Clock())
    r = run_row(conn, run_id)
    assert (r["lasta"], r["produktsidor"], r["resultat"]) == (3, 0, "delvis")
    assert product(conn, "B07V5J9RNV")["senaste_status"] == "pris"

def test_list_captcha_stops_the_omgang_and_marks_it_blocked(conn):
    add_list(conn, "L1")
    add_list(conn, "L2")
    src = FakeSource()
    src.lists["L1"] = ListRead(http_status=503)
    src.lists["L2"] = ok(item("A000000001", 100))
    res = collect(conn, src, now=Clock())
    assert res.blocked and len(res.run_ids) == 1


def test_product_page_captcha_marks_the_omgang_blocked(conn):
    local_watch(conn, "A000000001")
    src = FakeSource()
    src.products["A000000001"] = ProductRead(200, captcha=True)
    assert collect(conn, src, now=Clock()).blocked

def test_product_page_202_stops_product_pages_and_blocks(conn):
    local_watch(conn, "A000000001", "A000000002")
    src = FakeSource()
    src.products["A000000001"] = ProductRead(202)
    assert collect(conn, src, now=Clock()).blocked
    assert src.product_calls == ["A000000001"]
    text = conn.execute("SELECT text FROM event WHERE typ = 'http_fel'").fetchone()[0]
    assert text == "produktsida: HTTP 202 (JS-utmaning)"


@pytest.mark.parametrize("status", [404, 500])
def test_other_product_page_errors_are_not_blocking(conn, status):
    local_watch(conn, "A000000001")
    src = FakeSource()
    src.products["A000000001"] = ProductRead(status)
    assert not collect(conn, src, now=Clock()).blocked


@pytest.mark.parametrize("status", [202, 503])
def test_refused_list_blocks_the_omgang(conn, status):
    add_list(conn, "L1")
    src = FakeSource()
    src.lists["L1"] = ListRead(http_status=status)
    assert collect(conn, src, now=Clock()).blocked
    assert conn.execute("SELECT status FROM wishlist").fetchone()[0] == "blockerad"


@pytest.mark.parametrize("status", [404, 500])
def test_other_list_errors_are_not_blocking(conn, status):
    add_list(conn, "L1")
    src = FakeSource()
    src.lists["L1"] = ListRead(http_status=status)
    assert not collect(conn, src, now=Clock()).blocked


def test_404_on_a_list_is_not_blocking(conn):
    add_list(conn, "L1")
    add_list(conn, "L2")
    src = FakeSource()
    src.lists["L1"] = ListRead(http_status=404)
    src.lists["L2"] = ok(item("A000000001", 100))
    res = collect(conn, src, now=Clock())
    assert not res.blocked and len(res.run_ids) == 2
