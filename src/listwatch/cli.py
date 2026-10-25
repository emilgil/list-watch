import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import scheduler
from .amazon import list_id_from_url
from .db import connect, latest_version, migrate, schema_version, utc_iso
from .fetcher import make_fetcher
from .parser import BASE_URL
from .probe import render_report, run_probe

DATA_DIR = Path(os.environ.get("LISTWATCH_DATA_DIR", "/var/lib/listwatch"))
DEFAULT_PROBE_DIR = DATA_DIR / "probe"
DEFAULT_DB = DATA_DIR / "listwatch.db"


def _probe(args: argparse.Namespace) -> int:
    if not _is_amazon_se(args.url):
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


def _is_amazon_se(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("amazon.se", "www.amazon.se")


def _needs_migrate(conn) -> bool:
    if schema_version(conn) < latest_version():
        print("Fel: databasen är inte uppgraderad, kör listwatch migrate först", file=sys.stderr)
        return True
    return False


def _add_list(args: argparse.Namespace) -> int:
    list_id = list_id_from_url(args.url)
    if not _is_amazon_se(args.url) or not list_id:
        print("Fel: ange en delad önskelista på amazon.se (…/hz/wishlist/ls/<id>)", file=sys.stderr)
        return 2
    if args.procent is not None and not 1 <= args.procent <= 99:
        print("Fel: --procent måste vara 1–99", file=sys.stderr)
        return 2
    conn = connect(args.db)
    try:
        if _needs_migrate(conn):
            return 2
        if conn.execute("SELECT 1 FROM wishlist WHERE list_id = ?", (list_id,)).fetchone():
            print(f"Fel: listan {list_id} finns redan", file=sys.stderr)
            return 1
        conn.execute(
            """INSERT INTO wishlist (typ, namn, url, list_id, standard_procent, standard_aktiv, skapad)
               VALUES ('amazon', ?, ?, ?, ?, ?, ?)""",
            (
                args.namn or list_id, f"{BASE_URL}/hz/wishlist/ls/{list_id}", list_id,
                args.procent, int(args.procent is not None), utc_iso(datetime.now(timezone.utc)),
            ),
        )
    finally:
        conn.close()
    print(f"Lista tillagd: {args.namn or list_id} ({list_id})")
    if args.procent is not None:
        print(f"Larm vid {args.procent} % under referenspriset")
    else:
        print("Inget standardlarm (ange --procent för att larma)")
    return 0


def _collect(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    try:
        if _needs_migrate(conn):
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
    a = sub.add_parser("add-list", help="Bevaka en delad önskelista på amazon.se")
    a.add_argument("url")
    a.add_argument("--namn", help="visningsnamn (standard: listans namn på Amazon efter första omgången)")
    a.add_argument("--procent", type=int, help="standardlarm: procent under referenspriset (1–99)")
    a.add_argument("--db", type=Path, default=DEFAULT_DB, help="sökväg till SQLite-databasen")
    a.set_defaults(func=_add_list)
    c = sub.add_parser("collect", help="Kör en insamlingsomgång nu (manuell)")
    c.add_argument("--db", type=Path, default=DEFAULT_DB, help="sökväg till SQLite-databasen")
    c.set_defaults(func=_collect)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
