"""Ops notifications per list: unreadable list and parse errors."""
from listwatch.collector import collect
from listwatch.parser import parse_wishlist_page
from listwatch.source import ListRead
from test_collector import Clock, FakeSource, add_list, conn, item, ok  # noqa: F401 (fixture)
from test_scheduler import FakeNotifier


def wishlist(conn):
    return dict(conn.execute("SELECT * FROM wishlist").fetchone())


def ops_events(conn):
    return [r[0] for r in conn.execute("SELECT text FROM event WHERE typ = 'drift_notis' ORDER BY id")]


def run(conn, src, clock, n):
    return collect(conn, src, now=clock, notifier=n)


def items(count, price=10000):
    return [item(f"A{i:09d}", price) for i in range(count)]


def test_unreadable_list_notifies_after_two_omgangar_and_on_recovery(conn):
    add_list(conn)
    src, clock, n = FakeSource(), Clock(), FakeNotifier()
    src.lists["L1"] = ok(*items(3))
    run(conn, src, clock, n)
    src.lists["L1"] = ListRead(http_status=200, pages=1)  # private, removed or emptied
    for _ in range(3):
        run(conn, src, clock, n)
    assert wishlist(conn)["status"] == "privat" and wishlist(conn)["fel_i_rad"] == 3
    assert len(n.sent) == 1 and "går inte att läsa" in n.sent[0][0]
    assert "kan ha gjorts privat" in n.sent[0][1]
    src.lists["L1"] = ok(*items(3))
    run(conn, src, clock, n)
    assert wishlist(conn)["status"] == "ok" and wishlist(conn)["fel_i_rad"] == 0
    assert "går att läsa igen" in n.sent[1][0]
    assert len(ops_events(conn)) == 2


def test_single_failure_does_not_notify_and_recovers_quietly(conn):
    add_list(conn)
    src, clock, n = FakeSource(), Clock(), FakeNotifier()
    src.lists["L1"] = ListRead(http_status=500)
    run(conn, src, clock, n)
    assert wishlist(conn)["status"] == "fel"
    src.lists["L1"] = ok(*items(3))
    run(conn, src, clock, n)
    assert n.sent == []


def test_blocked_reads_do_not_count_as_unreadable(conn):
    add_list(conn)
    src, clock, n = FakeSource(), Clock(), FakeNotifier()
    src.lists["L1"] = ListRead(http_status=404)
    run(conn, src, clock, n)
    src.lists["L1"] = ListRead(http_status=503)
    run(conn, src, clock, n)
    assert wishlist(conn)["fel_i_rad"] == 1 and n.sent == []
    src.lists["L1"] = ListRead(http_status=404)
    run(conn, src, clock, n)
    assert wishlist(conn)["fel_i_rad"] == 2 and len(n.sent) == 1
    assert "HTTP" not in n.sent[0][1] and "privat" in n.sent[0][1]  # 404 counts as gone


def test_prices_lost_at_once_is_a_parse_error_once(conn):
    add_list(conn)
    src, clock, n = FakeSource(), Clock(), FakeNotifier()
    src.lists["L1"] = ok(*items(10))
    run(conn, src, clock, n)
    src.lists["L1"] = ok(*items(7), *(item(f"A{i:09d}") for i in range(7, 10)))  # 3 of 10 lose their price
    [run_id] = run(conn, src, clock, n)
    assert len(n.sent) == 1 and "parserfel" in n.sent[0][0]
    assert "3 av 10 produkter kunde inte tolkas (3 tappade sitt pris)" in n.sent[0][1]
    r = conn.execute("SELECT fel, resultat FROM run WHERE id = ?", (run_id,)).fetchone()
    assert tuple(r) == (1, "delvis")
    assert wishlist(conn)["parserfel"] == 1
    # still missing next time, but no longer "lost this omgång": back under, flag cleared
    run(conn, src, clock, n)
    assert wishlist(conn)["parserfel"] == 0 and len(n.sent) == 1


def test_unparsed_items_keep_the_flag_without_renotifying(conn):
    add_list(conn)
    src, clock, n = FakeSource(), Clock(), FakeNotifier()
    read = ok(*items(4))
    read.unparsed = 3
    src.lists["L1"] = read
    run(conn, src, clock, n)
    run(conn, src, clock, n)
    assert len(n.sent) == 1 and "(3 utan ASIN)" in n.sent[0][1]
    assert wishlist(conn)["parserfel"] == 1


def test_small_list_losing_a_price_is_not_a_parse_error(conn):
    add_list(conn)
    src, clock, n = FakeSource(), Clock(), FakeNotifier()
    src.lists["L1"] = ok(*items(2))
    run(conn, src, clock, n)
    src.lists["L1"] = ok(item("A000000000"), item("A000000001"))  # 2 of 2 lose their price
    run(conn, src, clock, n)
    assert n.sent == []


def test_parser_counts_items_without_asin():
    html = """<ul>
      <li data-itemid="1" data-reposition-action-params='{"itemExternalId":"ASIN:B000000001|A1"}'>
        <a id="itemName_1" title="Ett">Ett</a></li>
      <li data-itemid="2"><a id="itemName_2" title="Två">Två</a></li>
    </ul>"""
    page = parse_wishlist_page(html)
    assert [i.asin for i in page.items] == ["B000000001"] and page.unparsed == 1
