import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import scheduler
from .db import connect, latest_version, migrate, schema_version
from .fetcher import make_fetcher
from .probe import render_report, run_probe

DATA_DIR = Path(os.environ.get("LISTWATCH_DATA_DIR", "/var/lib/listwatch"))
DEFAULT_PROBE_DIR = DATA_DIR / "probe"
DEFAULT_DB = DATA_DIR / "listwatch.db"


def _probe(args: argparse.Namespace) -> int:
    host = urlparse(args.url).hostname or ""
    if host not in ("amazon.se", "www.amazon.se"):
        print("Fel: probe stöder bara amazon.se-länkar", file=sys.stderr)
        return 2
    report = run_probe(args.url, make_fetcher(), out_dir=Path(args.out))
    print(render_report(report))
    return report.exit_code


def _migrate(args: argparse.Namespace) -> int:
    try:
        res = migrate(args.db)
    except Exception as e:
        print(f"Fel: migrering misslyckades: {e}", file=sys.stderr)
        return 1
    print(f"Databas: {args.db}")
    if res.from_version == res.to_version:
        print(f"Schemat är redan aktuellt (v{res.to_version})")
    else:
        print(f"Schema: v{res.from_version} → v{res.to_version}")
    if res.backup:
        print(f"Säkerhetskopia: {res.backup}")
    return 0


def _collect(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    try:
        if schema_version(conn) < latest_version():
            print("Fel: databasen är inte uppgraderad, kör listwatch migrate först", file=sys.stderr)
            return 2
        res = scheduler.run_omgang(conn, scheduler.amazon_source(), typ="manuell")
        rows = [
            conn.execute("SELECT list_namn, lasta, larm_skickade, resultat FROM run WHERE id = ?", (i,)).fetchone()
            for i in res
        ]
    finally:
        conn.close()
    print(f"Omgång {res.omgang}")
    for r in rows:
        print(f"  {r['list_namn']}: {r['resultat'].upper()}, {r['lasta']} lästa, {r['larm_skickade']} larm")
    if res.blocked:
        print("  Blockerad av Amazon (CAPTCHA eller HTTP 503)")
    results = {r["resultat"] for r in rows}
    if res.blocked or "fel" in results:
        return 2
    return 1 if "delvis" in results else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="listwatch")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe", help="Läs en delad önskelista och rapportera vad som är läsbart")
    p.add_argument("url")
    p.add_argument("--out", default=DEFAULT_PROBE_DIR, help="katalog för sparad HTML och rapport")
    p.set_defaults(func=_probe)
    m = sub.add_parser("migrate", help="Skapa eller uppgradera databasschemat")
    m.add_argument("--db", type=Path, default=DEFAULT_DB, help="sökväg till SQLite-databasen")
    m.set_defaults(func=_migrate)
    c = sub.add_parser("collect", help="Kör en insamlingsomgång nu (manuell)")
    c.add_argument("--db", type=Path, default=DEFAULT_DB, help="sökväg till SQLite-databasen")
    c.set_defaults(func=_collect)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
