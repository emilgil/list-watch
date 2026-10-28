import random
import threading
from datetime import timedelta

import pytest

from listwatch import cli, scheduler
from listwatch.collector import CollectResult
from listwatch.db import connect, migrate, set_setting, setting, setting_int, utc_iso
from listwatch.scheduler import Scheduler, after_omgang, interval, next_start, run_omgang
from listwatch.source import ListRead
from test_collector import T0, Clock, FakeSource, add_list, item, ok


class FakeNotifier:
    def __init__(self, error=None):
        self.error = error
        self.sent: list[tuple[str, str]] = []

    def send(self, title, body):
        self.sent.append((title, body))
        return self.error


class NoDelay(random.Random):
    def uniform(self, a, b):
        return a


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


def blocked(run_ids=(1,)):
    return CollectResult(omgang=1, run_ids=list(run_ids), blocked=True)


def test_default_interval_is_six_hours(conn):
    assert interval(conn, T0) == timedelta(hours=6)


def test_interval_never_below_one_hour(conn):
    set_setting(conn, "intervall_timmar", 0)
    assert interval(conn, T0) == timedelta(hours=1)


def test_campaign_interval_applies_until_its_end(conn):
    set_setting(conn, "kampanj_intervall_timmar", 2)
    set_setting(conn, "kampanj_slut", utc_iso(T0 + timedelta(days=1)))
    assert interval(conn, T0) == timedelta(hours=2)
    assert interval(conn, T0 + timedelta(days=2)) == timedelta(hours=6)


def test_campaign_without_end_is_ignored(conn):
    set_setting(conn, "kampanj_intervall_timmar", 2)
    assert interval(conn, T0) == timedelta(hours=6)


def test_random_delay_stays_within_setting(conn):
    rng = random.Random(1)
    for _ in range(50):
        wait = next_start(conn, T0, rng) - T0
        assert timedelta(hours=6) <= wait <= timedelta(hours=6, minutes=30)


def test_backoff_doubles_and_is_capped(conn):
    waits = []
    for n in range(4):
        set_setting(conn, "blockerade_i_rad", n)
        waits.append(next_start(conn, T0, NoDelay()) - T0)
    assert waits == [timedelta(hours=h) for h in (6, 12, 24, 24)]


def test_backoff_never_shortens_a_long_interval(conn):
    set_setting(conn, "intervall_timmar", 48)
    set_setting(conn, "blockerade_i_rad", 1)
    assert next_start(conn, T0, NoDelay()) - T0 == timedelta(hours=48)


def test_ops_notification_once_at_three_blocked(conn):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ListRead(http_status=503)
    n = FakeNotifier()
    runs = [run_omgang(conn, src, now=Clock(), notifier=n).run_ids[0] for _ in range(4)]
    assert setting_int(conn, "blockerade_i_rad") == 4
    assert len(n.sent) == 1
    rows = conn.execute("SELECT run_id, text FROM event WHERE typ = 'drift_notis'").fetchall()
    assert [r["run_id"] for r in rows] == [runs[2]]
    assert "3 omgångar i rad" in rows[0]["text"]


def test_failed_ops_notification_is_logged(conn):
    for _ in range(3):
        after_omgang(conn, blocked(run_ids=()), T0, FakeNotifier(error="nere"))
    row = conn.execute("SELECT run_id, text FROM event WHERE typ = 'drift_notis'").fetchone()
    assert row["run_id"] is None and row["text"].endswith("Misslyckades: nere")


def test_unblocked_omgang_resets_the_count(conn):
    set_setting(conn, "blockerade_i_rad", 2)
    after_omgang(conn, CollectResult(omgang=1, run_ids=[1], blocked=False), T0, FakeNotifier())
    assert setting_int(conn, "blockerade_i_rad") == 0


def test_run_omgang_counts_a_blocked_list(conn):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ListRead(http_status=503)
    res = run_omgang(conn, src, now=Clock(), notifier=FakeNotifier())
    assert res.blocked and setting_int(conn, "blockerade_i_rad") == 1


def test_start_keeps_a_stored_future_start(db_path, conn):
    at = T0 + timedelta(hours=3)
    set_setting(conn, "nasta_korning", utc_iso(at))
    s = Scheduler(db_path, source_factory=FakeSource, now=lambda: T0)
    s.start(paused=True)
    try:
        assert s.next_run() == at
        assert s._aps.get_job("rensning") is not None
    finally:
        s.shutdown()


@pytest.mark.parametrize("stored", ["", utc_iso(T0 - timedelta(hours=1))])
def test_start_runs_soon_when_nothing_or_a_missed_start_is_stored(db_path, conn, stored):
    set_setting(conn, "nasta_korning", stored)
    s = Scheduler(db_path, source_factory=FakeSource, now=lambda: T0)
    s.start(paused=True)
    try:
        assert s.next_run() == T0 + scheduler.STARTUP_DELAY
    finally:
        s.shutdown()


def test_scheduled_job_runs_and_plans_the_next(db_path, conn):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 100))
    set_setting(conn, "fordrojning_minuter", 0)
    s = Scheduler(db_path, source_factory=lambda: src, notifier=FakeNotifier(), now=lambda: T0)
    s.start(paused=True)
    try:
        s._scheduled()
        assert conn.execute("SELECT typ, resultat FROM run").fetchall()[0][:] == ("schemalagd", "ok")
        assert setting(conn, "nasta_korning") == utc_iso(T0 + timedelta(hours=6))
        assert s.next_run() == T0 + timedelta(hours=6)
    finally:
        s.shutdown()


def test_scheduled_job_plans_the_next_even_if_the_omgang_crashes(db_path, conn):
    add_list(conn)

    class Broken(FakeSource):
        def read_list(self, url):
            raise RuntimeError("boom")

    s = Scheduler(db_path, source_factory=Broken, notifier=FakeNotifier(), now=lambda: T0)
    s.start(paused=True)
    try:
        s._scheduled()
        assert setting(conn, "nasta_korning") != ""
    finally:
        s.shutdown()


def test_manual_omgang_plans_the_next_start_and_lifts_backoff(db_path, conn):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 100))
    set_setting(conn, "fordrojning_minuter", 0)
    set_setting(conn, "blockerade_i_rad", 3)
    set_setting(conn, "nasta_korning", utc_iso(T0 + timedelta(hours=24)))
    s = Scheduler(db_path, source_factory=lambda: src, notifier=FakeNotifier(), now=lambda: T0)
    s.start(paused=True)
    try:
        s.run_now()
        assert setting_int(conn, "blockerade_i_rad") == 0
        assert s.next_run() == T0 + timedelta(hours=6)
        assert setting(conn, "nasta_korning") == utc_iso(T0 + timedelta(hours=6))
    finally:
        s.shutdown()


def test_skipped_scheduled_start_leaves_planning_to_the_running_omgang(db_path, conn):
    add_list(conn)
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeSource):
        def read_list(self, url):
            entered.set()
            release.wait(5)
            return ok(item("A000000001", 100))

    set_setting(conn, "fordrojning_minuter", 0)
    s = Scheduler(db_path, source_factory=Slow, notifier=FakeNotifier(), now=lambda: T0)
    s.start(paused=True)
    try:
        t = threading.Thread(target=s.run_now)
        t.start()
        entered.wait(5)
        s._scheduled()  # skipped
        release.set()
        t.join(5)
        assert s.next_run() == T0 + timedelta(hours=6)
    finally:
        s.shutdown()


def test_only_one_omgang_at_a_time(db_path, conn):
    add_list(conn)
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeSource):
        def read_list(self, url):
            entered.set()
            release.wait(5)
            return ok(item("A000000001", 100))

    s = Scheduler(db_path, source_factory=Slow, notifier=FakeNotifier(), now=lambda: T0)
    t = threading.Thread(target=s.run_now)
    t.start()
    entered.wait(5)
    assert s.run_now() is None
    release.set()
    t.join(5)
    assert s.run_now() is not None


def test_cli_collect(db_path, conn, monkeypatch, capsys):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 100))
    monkeypatch.setattr(scheduler, "amazon_source", lambda on_fetch=None, on_wait=None, cookie_file=None: src)
    monkeypatch.setattr(scheduler, "notifier_from_settings", lambda conn: FakeNotifier())
    assert cli.main(["collect", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "Omgång 1" in out and "L: OK, 1 lästa, 0 larm" in out
    assert conn.execute("SELECT typ FROM run").fetchone()[0] == "manuell"


def test_cli_collect_blocked_exits_2(db_path, conn, monkeypatch, capsys):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ListRead(http_status=503)
    monkeypatch.setattr(scheduler, "amazon_source", lambda on_fetch=None, on_wait=None, cookie_file=None: src)
    monkeypatch.setattr(scheduler, "notifier_from_settings", lambda conn: FakeNotifier())
    assert cli.main(["collect", "--db", str(db_path)]) == 2
    assert "Blockerad" in capsys.readouterr().out


def test_cli_collect_requires_migrated_db(tmp_path, capsys):
    assert cli.main(["collect", "--db", str(tmp_path / "new.db")]) == 2
    assert "listwatch migrate" in capsys.readouterr().err


def test_cli_collect_verbose_prints_each_item(db_path, conn, monkeypatch, capsys):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 129900, title="Kaffekvarn"), item("A000000002"))
    monkeypatch.setattr(scheduler, "amazon_source", lambda on_fetch=None, on_wait=None, cookie_file=None: src)
    monkeypatch.setattr(scheduler, "notifier_from_settings", lambda conn: FakeNotifier())
    cli.main(["collect", "--db", str(db_path), "--verbose"])
    out = capsys.readouterr().out
    assert "L1:\n" in out
    lines = [" ".join(l.split()) for l in out.splitlines()]
    assert "A000000001 1 299 kr Kaffekvarn [ny]" in lines
    assert "A000000002 saknar pris A000000002 [ny]" in lines


def test_cli_collect_is_quiet_without_verbose(db_path, conn, monkeypatch, capsys):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 100))
    monkeypatch.setattr(scheduler, "amazon_source", lambda on_fetch=None, on_wait=None, cookie_file=None: src)
    monkeypatch.setattr(scheduler, "notifier_from_settings", lambda conn: FakeNotifier())
    cli.main(["collect", "--db", str(db_path)])
    assert "A000000001" not in capsys.readouterr().out


def test_verbose_notes_price_change_and_fetches(conn):
    add_list(conn)
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 10000))
    clock = Clock()
    run_omgang(conn, src, now=clock, notifier=FakeNotifier())
    src.lists["L1"] = ok(item("A000000001", 9000))
    lines = []
    run_omgang(conn, src, now=clock, notifier=FakeNotifier(), progress=lines.append)
    assert any("90 kr" in l and "[var 100 kr]" in l for l in lines)


def test_amazon_source_reports_each_fetch(monkeypatch):
    from listwatch.fetcher import FetchResult

    monkeypatch.setattr(scheduler, "make_fetcher", lambda **kw: lambda url: FetchResult(404, "", 0.0))
    monkeypatch.setattr(scheduler, "polite", lambda f: f)
    monkeypatch.setattr("listwatch.amazon.PRODUCT_GROUP_GAP", (0, 0))
    urls = []
    scheduler.amazon_source(urls.append).read_product("B0C93G2M83")
    assert urls == ["https://www.amazon.se/dp/B0C93G2M83"]
