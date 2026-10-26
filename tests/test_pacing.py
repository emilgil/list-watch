"""AmazonSource spreads its requests out and retries a refused list read once."""
from listwatch import amazon
from listwatch.amazon import AmazonSource
from listwatch.collector import collect
from listwatch.fetcher import FetchResult
from test_collector import Clock, add_list, conn  # noqa: F401 (fixture)
from test_scheduler import FakeNotifier

LIST = (
    '<ul><li data-itemid="1" data-reposition-action-params=\'{"itemExternalId":"ASIN:B000000001|A1"}\'>'
    '<a id="itemName_1" title="Ett">Ett</a><span class="a-price"><span class="a-offscreen">100,00 kr</span></span>'
    "</li></ul>"
)


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.log: list[str] = []

    def fetch(self, url):
        self.log.append("fetch")
        status, html = self.responses.pop(0) if self.responses else (200, LIST)
        return FetchResult(status, html, 0.1)

    def sleep(self, seconds):
        self.log.append(f"sleep {seconds:.0f}")


def source(rec, waits=None):
    return AmazonSource(rec.fetch, sleep=rec.sleep, on_wait=waits.append if waits is not None else None)


def test_first_list_starts_at_once_later_lists_wait():
    rec = Recorder([])
    src = source(rec)
    src.read_list("https://www.amazon.se/hz/wishlist/ls/A")
    src.read_list("https://www.amazon.se/hz/wishlist/ls/B")
    assert rec.log[0] == "fetch" and rec.log[1].startswith("sleep")
    lo, hi = amazon.LIST_GAP
    assert lo <= float(rec.log[1].split()[1]) <= hi


def test_product_pages_come_in_small_groups_with_gaps():
    rec = Recorder([])
    src = source(rec)
    for n in range(12):
        src.read_product(f"B{n:09d}")
    groups, current = [], 0
    for entry in rec.log:
        if entry.startswith("sleep"):
            if current:
                groups.append(current)
            current = 0
        else:
            current += 1
    groups.append(current)
    assert rec.log[0].startswith("sleep")  # a gap after the list pages, before the first group
    assert sum(groups) == 12 and all(amazon.PRODUCT_GROUP[0] <= g <= amazon.PRODUCT_GROUP[1] for g in groups)


def test_a_new_list_starts_a_new_product_group():
    rec = Recorder([])
    src = source(rec)
    src._group_left = 2  # mid-group
    src.read_list("https://www.amazon.se/hz/wishlist/ls/A")
    rec.log.clear()
    src.read_product("B000000001")
    assert rec.log[0].startswith("sleep")


def test_refused_list_is_retried_once_after_a_pause():
    rec, waits = Recorder([(503, "")]), []
    read = source(rec, waits).read_list("https://www.amazon.se/hz/wishlist/ls/A")
    assert rec.log[:3] == ["fetch", rec.log[1], "fetch"] and rec.log[1].startswith("sleep")
    lo, hi = amazon.RETRY_WAIT
    assert lo <= float(rec.log[1].split()[1]) <= hi
    assert read.http_status == 200 and read.retried_after == "HTTP 503" and len(read.items) == 1
    assert "försöker igen (HTTP 503)" in waits[0]


def test_only_one_retry_per_omgang():
    rec = Recorder([(503, ""), (503, ""), (503, "")])
    src = source(rec)
    assert src.read_list("https://www.amazon.se/hz/wishlist/ls/A").http_status == 503
    assert rec.log.count("fetch") == 2
    src.read_list("https://www.amazon.se/hz/wishlist/ls/B")
    assert rec.log.count("fetch") == 3  # no second retry


def test_other_errors_are_not_retried():
    rec = Recorder([(404, "")])
    read = source(rec).read_list("https://www.amazon.se/hz/wishlist/ls/A")
    assert read.http_status == 404 and rec.log == ["fetch"] and read.retried_after is None


def test_a_successful_retry_is_logged_but_not_blocked(conn):
    add_list(conn)
    rec = Recorder([(503, "")])
    src = AmazonSource(rec.fetch, sleep=rec.sleep)
    [run_id] = collect(conn, _Urls(src), now=Clock(), notifier=FakeNotifier())
    r = conn.execute("SELECT resultat, fel FROM run WHERE id = ?", (run_id,)).fetchone()
    assert tuple(r) == ("ok", 0)
    ev = conn.execute("SELECT typ, text FROM event WHERE run_id = ? AND typ = 'http_fel'", (run_id,)).fetchone()
    assert ev["text"] == "listsida: HTTP 503, nytt försök efter paus"
    assert conn.execute("SELECT status FROM wishlist").fetchone()[0] == "ok"


class _Urls:
    """add_list stores 'L1' as URL; send it to amazon.se."""

    def __init__(self, src):
        self.src = src

    def read_list(self, url):
        return self.src.read_list(f"https://www.amazon.se/hz/wishlist/ls/{url}")

    def read_product(self, asin):
        return self.src.read_product(asin)
