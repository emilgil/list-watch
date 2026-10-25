"""SQLite access: connection setup, schema migrations and run-log purging."""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Callable

NOTIFICATION_EVENTS = ("larm_skickat", "larm_misslyckades", "drift_notis", "test_notis")


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str) -> sqlite3.Connection:
    # Autocommit; callers open transactions explicitly.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migrations() -> list[tuple[int, str]]:
    """(version, sql) for every migrations/NNNN_*.sql, in order."""
    folder = resources.files("listwatch") / "migrations"
    found = [
        (int(f.name[:4]), f.read_text(encoding="utf-8"))
        for f in folder.iterdir()
        if f.name.endswith(".sql")
    ]
    return sorted(found)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


@dataclass
class MigrateResult:
    from_version: int
    to_version: int
    backup: Path | None


def migrate(path: Path | str, now: Callable[[], datetime] = datetime.now) -> MigrateResult:
    """Bring the database at `path` to the latest schema, backing it up before an upgrade."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        current = schema_version(conn)
        migrations = _migrations()
        latest = migrations[-1][0] if migrations else 0
        if current > latest:
            raise RuntimeError(f"databasen har schema v{current}, koden känner bara till v{latest}")
        pending = [(v, sql) for v, sql in migrations if v > current]
        backup = None
        if pending and current > 0:
            backup = path.with_name(f"{path.stem}.v{current}-{now():%Y%m%d-%H%M%S}.bak")
            dst = sqlite3.connect(backup)
            try:
                conn.backup(dst)
            finally:
                dst.close()
        for version, sql in pending:
            try:
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return MigrateResult(current, schema_version(conn), backup)
    finally:
        conn.close()


def setting_int(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT varde FROM settings WHERE nyckel = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return int(row["varde"])


@dataclass
class PurgeResult:
    events: int
    runs: int


def purge_logs(conn: sqlite3.Connection, now: datetime) -> PurgeResult:
    """Delete runs and events past their retention. Price history is never touched."""
    log_cutoff = utc_iso(now - timedelta(days=setting_int(conn, "logg_lagringstid_dagar")))
    notis_cutoff = utc_iso(now - timedelta(days=setting_int(conn, "notislogg_lagringstid_dagar")))
    marks = ", ".join("?" * len(NOTIFICATION_EVENTS))
    conn.execute("BEGIN")
    try:
        events = conn.execute(
            f"DELETE FROM event WHERE typ NOT IN ({marks}) AND tid < ?",
            (*NOTIFICATION_EVENTS, log_cutoff),
        ).rowcount
        events += conn.execute(
            f"DELETE FROM event WHERE typ IN ({marks}) AND tid < ?",
            (*NOTIFICATION_EVENTS, notis_cutoff),
        ).rowcount
        # Remaining notification events keep their row with run_id = NULL (ON DELETE SET NULL).
        runs = conn.execute("DELETE FROM run WHERE start < ?", (log_cutoff,)).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return PurgeResult(events=events, runs=runs)
