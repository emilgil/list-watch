import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from listwatch import lists, webdata
from listwatch.db import connect, migrate, set_setting, setting, utc_iso
from listwatch.scheduler import Scheduler, run_omgang
from listwatch.web import create_app
from test_collector import T0, Clock, FakeSource, item, ok
from test_scheduler import FakeNotifier

URL = "https://www.amazon.se/hz/wishlist/ls/3EWS08VXURE1W"
A = "B000000001"
B = "B000000002"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "lw.db"
    migrate(path)
    return path


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def client(db_path, notifier):
    with TestClient(create_app(db_path, notifier=notifier, now=lambda: T0 + timedelta(days=10))) as c:
        yield c


def seed(conn, procent=20):
    """A list with two products and ten days of Prime prices for A (1 000 kr, now 700 kr)."""
    wl = lists.add_amazon_list(conn, URL, "Hemmet", procent)
    src = FakeSource()
    src.lists[conn.execute("SELECT url FROM wishlist WHERE id = ?", (wl,)).fetchone()[0]] = ok(
        item(A, 100000, title="Kaffekvarn"), item(B, title="Citronpress")
    )
    run_omgang(conn, src, now=Clock(), notifier=FakeNotifier())
    run = conn.execute("SELECT max(id) FROM run").fetchone()[0]
    for day in range(1, 10):
        conn.execute(
            "INSERT INTO price_point (asin, tidpunkt, status, pris_ore, prime, kalla, run_id) VALUES (?, ?, 'pris', ?, 'prime', 'lista', ?)",
            (A, utc_iso(T0 + timedelta(days=day)), 100000 if day < 9 else 70000, run),
        )
    conn.execute("UPDATE product SET senaste_pris_ore = 70000, prime_status = 'prime' WHERE asin = ?", (A,))
    return wl


def text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# Översikt


def test_overview_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Inga produkter bevakas" in r.text


def test_overview_shows_price_reference_discount_and_trend(client, conn):
    seed(conn)
    page = text(client.get("/").text)
    assert "Kaffekvarn" in page and "700 kr" in page
    assert "ref 1 000 kr" in page and "-30 %" in page
    assert "↓" in page
    assert "Citronpress" in page and "saknar pris" in page


def test_overview_sorts_by_discount_and_filters_by_list(client, conn):
    wl = seed(conn)
    page = client.get("/").text
    assert page.index("Kaffekvarn") < page.index("Citronpress")
    page = client.get("/?sort=namn").text
    assert page.index("Citronpress") < page.index("Kaffekvarn")
    lists.add_asin(conn, "B000000003")
    assert "B000000003" not in client.get(f"/?lista={wl}").text
    assert "B000000003" in client.get("/").text


def test_overview_rows_compute_level(conn):
    seed(conn)
    rows = {r.asin: r for r in webdata.overview(conn, T0 + timedelta(days=10))}
    a = rows[A]
    assert a.level.reference.median_ore == 100000 and a.level.percent_level_ore == 80000
    assert round(a.rabatt) == 30 and a.trend == "ned" and a.larm_status == "bevakas"
    assert rows[B].pris_ore is None and rows[B].larm_status == "vantar"


def test_alert_toggle(client, conn):
    seed(conn)
    r = client.post(f"/produkt/{A}/larm")
    assert "Larm: av" in r.text
    assert conn.execute("SELECT larm_aktivt FROM alert_rule WHERE asin = ?", (A,)).fetchone()[0] == 0
    assert "Larm: på" in client.post(f"/produkt/{A}/larm").text
    assert client.post("/produkt/B999999999/larm").status_code == 404


# Produkt


def test_product_page_with_chart_and_stats(client, conn):
    seed(conn)
    r = client.get(f"/produkt/{A}")
    assert r.status_code == 200
    page = text(r.text)
    assert "Lägsta 700 kr" in page and "Högsta 1 000 kr" in page
    assert "20 % under referens (≤ 800 kr)" in page
    data = json.loads(re.search(r"const d = (\{.*?\});", r.text).group(1))
    assert data["pris"][-1] == 700 and data["referens"][0] == 1000 and data["procentniva"][0] == 800
    assert data["mal"] is None


def test_chart_has_gaps_for_unpriced_points(conn):
    seed(conn)
    view = webdata.product_view(conn, B, T0 + timedelta(days=10))
    assert webdata.chart_data(view)["pris"] == [None]


def test_product_period(client, conn):
    seed(conn)
    page = text(client.get(f"/produkt/{A}?period=30d").text)
    assert "Lägsta 700 kr" in page
    assert client.get("/produkt/B999999999").status_code == 404


def test_save_rule(client, conn):
    seed(conn)
    r = client.post(
        f"/produkt/{A}/regel",
        data={"mal_pris": "2 990,50", "mal_procent": "15", "larm_aktivt": "on", "lagsta_nagonsin": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    row = conn.execute("SELECT * FROM alert_rule WHERE asin = ?", (A,)).fetchone()
    assert (row["mal_pris_ore"], row["mal_procent"], row["arver_standard"]) == (299050, 15, 0)
    assert (row["lagsta_nagonsin_notis"], row["larm_aktivt"]) == (1, 1)
    page = text(client.get(f"/produkt/{A}").text)
    assert "mål 2 990,50 kr eller 15 % under referens" in page


def test_save_rule_keeps_suppression_and_inherits(client, conn):
    seed(conn)
    conn.execute("INSERT INTO alert_rule (asin, senaste_larm_pris) VALUES (?, 70000)", (A,))
    client.post(f"/produkt/{A}/regel", data={"arver_standard": "on", "larm_aktivt": "on"})
    row = conn.execute("SELECT * FROM alert_rule WHERE asin = ?", (A,)).fetchone()
    assert (row["senaste_larm_pris"], row["arver_standard"], row["mal_pris_ore"]) == (70000, 1, None)


@pytest.mark.parametrize("data", [{"mal_pris": "abc"}, {"mal_pris": "0"}, {"mal_procent": "100"}])
def test_save_rule_rejects_bad_input(client, conn, data):
    seed(conn)
    r = client.post(f"/produkt/{A}/regel", data=data, follow_redirects=False)
    assert "fel=" in r.headers["location"]
    assert conn.execute("SELECT count(*) FROM alert_rule").fetchone()[0] == 0


# Listor


def test_lists_page_add_list_and_asin(client, conn):
    r = client.post("/listor", data={"lank": URL + "?ref_=wl_share", "procent": "20"})
    assert "Listan 3EWS08VXURE1W är tillagd" in text(r.text)
    r = client.post("/listor", data={"lank": "https://www.amazon.se/dp/B0C93G2M83?th=1"})
    assert "B0C93G2M83 bevakas" in text(r.text)
    page = text(client.get("/listor").text)
    assert "3EWS08VXURE1W" in page and "Enstaka produkter" in page and "B0C93G2M83" in page
    assert [w["typ"] for w in conn.execute("SELECT typ FROM wishlist ORDER BY id")] == ["amazon", "lokal"]


@pytest.mark.parametrize("lank", ["https://example.com/x", URL.replace(".se", ".com")])
def test_add_rejects_non_amazon_se(client, conn, lank):
    r = client.post("/listor", data={"lank": lank}, follow_redirects=False)
    assert "fel=" in r.headers["location"]
    assert conn.execute("SELECT count(*) FROM wishlist").fetchone()[0] == 0


def test_add_duplicate(client):
    client.post("/listor", data={"lank": URL})
    assert "finns redan" in text(client.post("/listor", data={"lank": URL}).text)
    client.post("/listor", data={"lank": "B0C93G2M83"})
    assert "bevakas redan" in text(client.post("/listor", data={"lank": "B0C93G2M83"}).text)


def test_set_and_clear_default_percent(client, conn):
    wl = seed(conn)
    client.post(f"/listor/{wl}/procent", data={"procent": "30"})
    assert tuple(conn.execute("SELECT standard_procent, standard_aktiv FROM wishlist").fetchone()) == (30, 1)
    client.post(f"/listor/{wl}/procent", data={"procent": ""})
    assert tuple(conn.execute("SELECT standard_procent, standard_aktiv FROM wishlist").fetchone()) == (30, 0)


def test_remove_list(client, conn):
    wl = seed(conn)
    r = client.post(f"/listor/{wl}/ta-bort")
    assert "Hemmet är borttagen; 2 produkter" in text(r.text)
    assert conn.execute("SELECT count(*) FROM wishlist").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM price_point").fetchone()[0] > 0


def test_stop_watching_single_product(client, conn):
    lists.add_asin(conn, "B0C93G2M83")
    page = client.get("/produkt/B0C93G2M83").text
    assert "Sluta bevaka" in page
    client.post("/produkt/B0C93G2M83/sluta-bevaka")
    assert conn.execute("SELECT aktiv FROM product WHERE asin = 'B0C93G2M83'").fetchone()[0] == 0


def test_local_watch_gets_its_title_from_the_product_page(conn):
    from listwatch.parser import ProductPage
    from listwatch.source import ProductRead

    lists.add_asin(conn, "B0C93G2M83")
    src = FakeSource()
    src.products["B0C93G2M83"] = ProductRead(
        200, page=ProductPage(price_ore=11566, status="pris", prime=True, ships_from_amazon=True, title="USB4-kabel")
    )
    run_omgang(conn, src, now=Clock(), notifier=FakeNotifier())
    assert conn.execute("SELECT titel, senaste_pris_ore FROM product").fetchone()[:] == ("USB4-kabel", 11566)


# Synka nu


def test_sync_now_without_scheduler(client):
    assert "Schemaläggaren kör inte" in client.post("/synka").text


def test_sync_now_runs_an_omgang(db_path, conn):
    lists.add_amazon_list(conn, URL)
    src = FakeSource()
    src.lists[URL] = ok(item(A, 100))
    s = Scheduler(db_path, source_factory=lambda: src, notifier=FakeNotifier())
    app = create_app(db_path, s)
    client = TestClient(app)  # no lifespan: the scheduler is not started, run_now still works
    assert "Omgången har startat" in client.post("/synka").text
    app.state.sync_thread.join(5)
    assert conn.execute("SELECT typ, resultat FROM run").fetchone()[:] == ("manuell", "ok")


# Logg


def test_log_pages(client, conn):
    seed(conn)
    page = text(client.get("/logg").text)
    assert "Hemmet" in page and "DELVIS" in page
    run_id = conn.execute("SELECT max(id) FROM run").fetchone()[0]
    page = text(client.get(f"/logg/{run_id}").text)
    assert "ny produkt" in page and "Kaffekvarn" in page
    page = text(client.get(f"/logg/{run_id}?typ=saknar_pris").text)
    assert "Citronpress" in page and "Kaffekvarn" not in page
    assert client.get("/logg/999").status_code == 404


def test_log_filtered_by_product_and_type(client, conn):
    seed(conn)
    page = text(client.get(f"/logg?asin={A}").text)
    assert "Händelser: Kaffekvarn" in page and "Citronpress" not in page
    page = text(client.get("/logg?typ=saknar_pris").text)
    assert "Citronpress" in page


def test_price_values_in_events_show_as_kr(client, conn):
    seed(conn)
    conn.execute("INSERT INTO event (tid, typ, asin, fore, efter) VALUES (?, 'pris_ändrat', ?, '100000', '70000')", (utc_iso(T0), A))
    assert "1 000 kr → 700 kr" in text(client.get(f"/logg?asin={A}").text)


# Inställningar


SETTINGS = {
    "apprise_urls": " ntfy://a \n\n tgram://b/c ",
    "intervall_timmar": "4",
    "kampanj_intervall_timmar": "1",
    "kampanj_slut": "2026-11-30T23:59",
    "fordrojning_minuter": "15",
    "logg_lagringstid_dagar": "30",
    "notislogg_lagringstid_dagar": "365",
}


def test_settings_save(client, conn):
    r = client.post("/installningar", data=SETTINGS, follow_redirects=False)
    assert "ok=" in r.headers["location"]
    assert setting(conn, "apprise_urls") == "ntfy://a\ntgram://b/c"
    assert setting(conn, "intervall_timmar") == "4"
    assert setting(conn, "kampanj_slut") == "2026-11-30T22:59:00Z"  # Stockholm CET → UTC
    page = client.get("/installningar").text
    assert 'value="2026-11-30T23:59"' in page and "ntfy://a" in page


@pytest.mark.parametrize("key,value", [("intervall_timmar", "0"), ("fordrojning_minuter", "999"), ("kampanj_slut", "snart")])
def test_settings_validation(client, conn, key, value):
    r = client.post("/installningar", data={**SETTINGS, key: value}, follow_redirects=False)
    assert "fel=" in r.headers["location"]
    assert setting(conn, "intervall_timmar") == "6"


def test_settings_reschedule_only_when_the_schedule_changes(db_path, conn):
    s = Scheduler(db_path, source_factory=FakeSource)
    calls = []
    s.reschedule = lambda: calls.append(1) or T0
    s._aps.start(paused=True)
    try:
        client = TestClient(create_app(db_path, s))
        base = {**SETTINGS, "intervall_timmar": "6", "kampanj_intervall_timmar": "0", "kampanj_slut": "",
                "fordrojning_minuter": "30"}
        client.post("/installningar", data=base)
        assert calls == []
        client.post("/installningar", data={**base, "intervall_timmar": "3"})
        assert calls == [1]
    finally:
        s.shutdown()


def test_test_notice(client, conn, notifier):
    r = client.post("/installningar/testnotis")
    assert "Testnotisen är skickad" in r.text and len(notifier.sent) == 1
    notifier.error = "nere <b>"
    assert "Misslyckades: nere &lt;b&gt;" in client.post("/installningar/testnotis").text
    texts = [r[0] for r in conn.execute("SELECT text FROM event WHERE typ = 'test_notis' ORDER BY id")]
    assert texts == ["skickad", "misslyckades: nere <b>"]
    page = text(client.get("/installningar").text)
    assert "test notis" in page and "misslyckades: nere" in page


def test_static_files(client):
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/chart.umd.js").status_code == 200
