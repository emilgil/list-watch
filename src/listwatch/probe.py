"""Step 0: fetch a wishlist the way the service will and report what is readable."""
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .amazon import list_id_from_url, read_wishlist
from .fetcher import FetchResult, polite
from .parser import STATUS_MISSING_PRICE, STATUS_OUT_OF_STOCK

__all__ = ["FetchResult", "ProbeReport", "render_report", "run_probe"]

@dataclass
class ProbeReport:
    url: str
    list_id: str | None = None
    list_title: str | None = None
    http_status: int | None = None
    pages: int = 0
    avg_seconds: float = 0.0
    captcha: bool = False
    products: int = 0
    price_read: int = 0
    out_of_stock: int = 0
    missing_price: list[str] = field(default_factory=list)
    exit_code: int = 2
    saved_dir: str | None = None
    started: str = ""
    items: list[dict] = field(default_factory=list)


def run_probe(
    url: str,
    fetch: Callable[[str], FetchResult],
    out_dir: Path,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = datetime.now,
) -> ProbeReport:
    started = now()
    report = ProbeReport(url=url, started=started.strftime("%Y-%m-%d %H:%M"))
    report.list_id = list_id_from_url(url)
    saved = Path(out_dir) / started.strftime("%Y-%m-%d_%H%M")
    saved.mkdir(parents=True, exist_ok=True)
    report.saved_dir = str(saved)

    def save(n: int, html: str) -> None:
        (saved / f"page-{n}.html").write_text(html, encoding="utf-8")

    read = read_wishlist(url, polite(fetch, sleep), on_page=save)
    items = read.items
    report.http_status = read.http_status
    report.captcha = read.captcha
    report.pages = read.pages
    report.list_title = read.title
    report.avg_seconds = sum(read.seconds) / len(read.seconds) if read.seconds else 0.0
    report.products = len(items)
    report.price_read = sum(i.price_ore is not None for i in items)
    report.out_of_stock = sum(i.status == STATUS_OUT_OF_STOCK for i in items)
    report.missing_price = [i.asin for i in items if i.status == STATUS_MISSING_PRICE]
    report.items = [
        {"asin": i.asin, "title": i.title, "status": i.status, "price_ore": i.price_ore}
        for i in items
    ]
    if read.error:
        report.exit_code = 2
    else:
        report.exit_code = 1 if report.missing_price else 0
    (saved / "report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _pct(n: int, total: int) -> str:
    return f"{round(100 * n / total)} %" if total else "-"


def render_report(r: ProbeReport) -> str:
    if r.http_status == 202:
        http = "JS-utmaning (HTTP 202)"
    elif r.http_status:
        http = f"{r.http_status} {'OK' if r.http_status == 200 else ''}".strip()
    else:
        http = "ingen respons"
    lines = [
        f"List Watch – probe  {r.started}",
        f'Lista:        "{r.list_title or "?"}" ({r.list_id or "?"})',
        f"HTTP:         {http} ({r.pages} sidor, {r.avg_seconds:.1f} s/sida i snitt)",
        f"CAPTCHA:      {'JA' if r.captcha else 'nej'}",
        "",
        f"Produkter:    {r.products}",
        f"  Pris läst:        {r.price_read} / {r.products}  ({_pct(r.price_read, r.products)})",
        f"  Ej i lager:        {r.out_of_stock}",
        f"  Saknar pris:       {len(r.missing_price)}"
        + (f"  → {', '.join(r.missing_price)}" if r.missing_price else ""),
        "",
        f"Sparat:  {r.saved_dir}/",
        "",
    ]
    verdict = {0: "ALLT LÄSBART", 1: "DELVIS OK", 2: "BLOCKERAD ELLER FEL"}[r.exit_code]
    lines.append(f"Resultat: {verdict} (avslutskod {r.exit_code})")
    return "\n".join(lines)
