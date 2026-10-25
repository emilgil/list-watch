from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from listwatch import cli
from listwatch.db import backup, connect, migrate, set_setting, utc_iso
from listwatch.scheduler import Scheduler, run_omgang
from listwatch.web import create_app, local_time
from test_collector import T0, Clock, FakeSource, add_list, item, ok
from test_scheduler import FakeNotifier


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


def collect_once(conn, read=None):
    src = FakeSource()
    src.lists["L1"] = read or ok(item("A000000001", 100))
    return run_omgang(conn, src, now=Clock(), notifier=FakeNotifier())


def test_health_without_scheduler(db_path, conn):
    add_list(conn)
    collect_once(conn)
    with TestClient(create_app(db_path)) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["schemalaggare"] is False
    assert body["senaste_omgang"] == {"omgang": 1, "slut": utc_iso(T0), "resultat": "ok"}
    assert body["blockerade_i_rad"] == 0


def test_health_reports_worst_result_of_last_omgang(db_path, conn):
    add_list(conn, "L1")
    add_list(conn, "L2")
    src = FakeSource()
    src.lists["L1"] = ok(item("A000000001", 100))
    src.lists["L2"] = ok(item("A000000002"))
    run_omgang(conn, src, now=Clock(), notifier=FakeNotifier())
    with TestClient(create_app(db_path)) as client:
        assert client.get("/health").json()["senaste_omgang"]["resultat"] == "delvis"


def test_health_with_running_scheduler(db_path, conn):
    # A real (running) scheduler fires past starts at once, so plan in the real future.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    at = now + timedelta(days=1)
    set_setting(conn, "nasta_korning", utc_iso(at))
    s = Scheduler(db_path, source_factory=FakeSource, now=lambda: now)
    with TestClient(create_app(db_path, s)) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok" and body["schemalaggare"] is True
        assert body["nasta_korning"] == local_time(at)
        assert body["senaste_omgang"] is None
    assert not s.running


def test_health_fails_on_old_schema(db_path, conn):
    conn.execute("PRAGMA user_version = 1")
    with TestClient(create_app(db_path)) as client:
        r = client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "fel"


def test_index_lists_wishlists(db_path, conn):
    add_list(conn)
    collect_once(conn)
    with TestClient(create_app(db_path)) as client:
        r = client.get("/")
    assert r.status_code == 200
    # added under its list id, so it took the title Amazon reported ("L")
    assert "<td>L</td>" in r.text and "Senaste omgång 1:" in r.text and ">OK</span>" in r.text


def test_index_without_lists(db_path):
    with TestClient(create_app(db_path)) as client:
        assert "Inga listor bevakas" in client.get("/").text


def test_backup_keeps_the_newest(db_path, conn, tmp_path):
    add_list(conn)
    times = iter(T0 + timedelta(days=d) for d in range(4))
    dest = tmp_path / "backup"
    results = [backup(db_path, dest, keep=2, now=lambda: next(times).replace(tzinfo=None)) for _ in range(4)]
    names = sorted(p.name for p in dest.iterdir())
    assert names == ["lw-20261027-120000.db", "lw-20261028-120000.db"]
    assert [p.name for p in results[-1].removed] == ["lw-20261026-120000.db"]
    copy = connect(dest / names[-1])
    try:
        assert copy.execute("SELECT count(*) FROM wishlist").fetchone()[0] == 1
    finally:
        copy.close()


def test_cli_backup(db_path, capsys):
    assert cli.main(["backup", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "Säkerhetskopia:" in out
    assert len(list((db_path.parent / "backup").glob("lw-*.db"))) == 1


def test_cli_backup_missing_db(tmp_path, capsys):
    assert cli.main(["backup", "--db", str(tmp_path / "nope.db")]) == 1
    assert "misslyckades" in capsys.readouterr().err


def test_cli_serve_requires_migrated_db(tmp_path, capsys):
    assert cli.main(["serve", "--db", str(tmp_path / "new.db")]) == 2
    assert "listwatch migrate" in capsys.readouterr().err


def test_local_time_follows_dst():
    assert local_time("2026-10-25T00:30:00Z") == "2026-10-25 02:30"  # still CEST
    assert local_time("2026-10-25T12:00:00Z") == "2026-10-25 13:00"  # CET after the switch
