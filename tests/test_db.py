import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from listwatch import cli, db
from listwatch.db import connect, latest_version, migrate, purge_logs, schema_version, utc_iso

LATEST = latest_version()

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
T = utc_iso(NOW)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "listwatch.db"
    migrate(path)
    c = connect(path)
    yield c
    c.close()


def add_product(conn, asin="B0C93G2M83"):
    conn.execute("INSERT INTO product (asin, forsta_sedd) VALUES (?, ?)", (asin, T))


def test_fresh_migrate_creates_schema_without_backup(tmp_path):
    path = tmp_path / "sub" / "listwatch.db"
    res = migrate(path)
    assert LATEST >= 2
    assert (res.from_version, res.to_version, res.backup) == (0, LATEST, None)
    c = connect(path)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {
        "wishlist", "product", "wishlist_product", "price_point", "alert_rule",
        "run", "event", "settings",
    }
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.setting_int(c, "logg_lagringstid_dagar") == 30
    assert db.setting_int(c, "notislogg_lagringstid_dagar") == 365


def test_rerun_is_a_no_op(tmp_path):
    path = tmp_path / "listwatch.db"
    migrate(path)
    res = migrate(path)
    assert (res.from_version, res.to_version, res.backup) == (LATEST, LATEST, None)


def test_upgrade_backs_up_first(tmp_path, monkeypatch):
    path = tmp_path / "listwatch.db"
    migrate(path)
    real = db._migrations()
    monkeypatch.setattr(db, "_migrations", lambda: real + [(LATEST + 1, "CREATE TABLE extra (x);")])
    res = migrate(path, now=lambda: datetime(2026, 10, 25, 16, 30))
    assert (res.from_version, res.to_version) == (LATEST, LATEST + 1)
    assert res.backup.name == f"listwatch.v{LATEST}-20261025-163000.bak"
    backup = sqlite3.connect(res.backup)
    assert backup.execute("PRAGMA user_version").fetchone()[0] == LATEST


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "listwatch.db"
    migrate(path)
    real = db._migrations()
    monkeypatch.setattr(db, "_migrations", lambda: real + [(LATEST + 1, "CREATE TABLE ok (x); SELEKT;")])
    with pytest.raises(sqlite3.Error):
        migrate(path)
    c = connect(path)
    assert schema_version(c) == LATEST
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'ok'").fetchone()[0] == 0


def test_refuses_newer_schema_than_code(tmp_path):
    path = tmp_path / "listwatch.db"
    c = sqlite3.connect(path)
    c.execute("PRAGMA user_version = 99")
    c.close()
    with pytest.raises(RuntimeError, match="v99"):
        migrate(path)


def test_price_must_match_status(conn):
    add_product(conn)
    insert = "INSERT INTO price_point (asin, tidpunkt, status, pris_ore, prime, kalla) VALUES (?, ?, ?, ?, 'okänd', 'lista')"
    conn.execute(insert, ("B0C93G2M83", T, "pris", 11566))
    conn.execute(insert, ("B0C93G2M83", T, "ej_i_lager", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("B0C93G2M83", T, "pris", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("B0C93G2M83", T, "saknar_pris", 100))


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO price_point (asin, tidpunkt, status, prime, kalla) VALUES ('B000000000', ?, 'saknar_pris', 'okänd', 'lista')",
            (T,),
        )


def add_run(conn, start):
    return conn.execute(
        "INSERT INTO run (omgang, typ, start) VALUES (1, 'schemalagd', ?)", (utc_iso(start),)
    ).lastrowid


def add_event(conn, run_id, typ, tid):
    conn.execute("INSERT INTO event (run_id, tid, typ) VALUES (?, ?, ?)", (run_id, utc_iso(tid), typ))


def test_purge_uses_separate_retention_for_notifications(conn):
    old = NOW - timedelta(days=31)
    ancient = NOW - timedelta(days=366)
    old_run = add_run(conn, old)
    new_run = add_run(conn, NOW)
    add_event(conn, old_run, "pris_ändrat", old)
    add_event(conn, old_run, "larm_skickat", old)
    add_event(conn, None, "test_notis", ancient)
    add_event(conn, new_run, "pris_ändrat", NOW)

    res = purge_logs(conn, NOW)

    assert (res.events, res.runs) == (2, 1)
    rows = conn.execute("SELECT run_id, typ FROM event ORDER BY id").fetchall()
    # The 31-day-old alert survives its run, now detached from it
    assert [tuple(r) for r in rows] == [(None, "larm_skickat"), (new_run, "pris_ändrat")]
    assert [r[0] for r in conn.execute("SELECT id FROM run")] == [new_run]


def test_purge_never_touches_price_history(conn):
    add_product(conn)
    run_id = add_run(conn, NOW - timedelta(days=100))
    conn.execute(
        "INSERT INTO price_point (asin, tidpunkt, status, pris_ore, prime, kalla, run_id) VALUES ('B0C93G2M83', ?, 'pris', 100, 'okänd', 'lista', ?)",
        (utc_iso(NOW - timedelta(days=100)), run_id),
    )
    purge_logs(conn, NOW)
    assert [tuple(r) for r in conn.execute("SELECT pris_ore, run_id FROM price_point")] == [(100, None)]


def test_purge_follows_retention_setting(conn):
    conn.execute("UPDATE settings SET varde = '7' WHERE nyckel = 'logg_lagringstid_dagar'")
    add_run(conn, NOW - timedelta(days=8))
    assert purge_logs(conn, NOW).runs == 1


def test_cli_migrate(tmp_path, capsys):
    path = tmp_path / "listwatch.db"
    assert cli.main(["migrate", "--db", str(path)]) == 0
    assert f"v0 → v{LATEST}" in capsys.readouterr().out
    assert cli.main(["migrate", "--db", str(path)]) == 0
    assert f"redan aktuellt (v{LATEST})" in capsys.readouterr().out
