import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

from .fetcher import make_fetcher
from .probe import render_report, run_probe

DEFAULT_PROBE_DIR = "/var/lib/listwatch/probe"


def _probe(args: argparse.Namespace) -> int:
    host = urlparse(args.url).hostname or ""
    if host not in ("amazon.se", "www.amazon.se"):
        print("Fel: probe stöder bara amazon.se-länkar", file=sys.stderr)
        return 2
    report = run_probe(args.url, make_fetcher(), out_dir=Path(args.out))
    print(render_report(report))
    return report.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="listwatch")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe", help="Läs en delad önskelista och rapportera vad som är läsbart")
    p.add_argument("url")
    p.add_argument("--out", default=DEFAULT_PROBE_DIR, help="katalog för sparad HTML och rapport")
    p.set_defaults(func=_probe)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
