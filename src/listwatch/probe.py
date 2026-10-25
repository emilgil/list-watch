"""Step 0: fetch a wishlist the way the service will and report what is readable."""
import json
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .parser import WishlistItem, is_captcha, parse_wishlist_page

MAX_PAGES = 50
DELAY_RANGE = (5, 15)
_LIST_ID = re.compile(r"/wishlist/ls/([A-Z0-9]+)")


@dataclass
class FetchResult:
    status: int
    html: str
    seconds: float


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
    prime_read: int = 0
    out_of_stock: int = 0
    missing_price: list[str] = field(default_factory=list)
    product_pages_needed: int = 0
    exit_code: int = 2
    saved_dir: str | None = None
    started: str = ""
    items: list[dict] = field(default_factory=list)


def _needs_product_page(i: WishlistItem) -> bool:
    return i.in_stock and (i.price_ore is None or i.prime is None)


def run_probe(
    url: str,
    fetch: Callable[[str], FetchResult],
    out_dir: Path,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = datetime.now,
) -> ProbeReport:
    started = now()
    report = ProbeReport(url=url, started=started.strftime("%Y-%m-%d %H:%M"))
    m = _LIST_ID.search(url)
    report.list_id = m.group(1) if m else None
    saved = Path(out_dir) / started.strftime("%Y-%m-%d_%H%M")
    saved.mkdir(parents=True, exist_ok=True)
    report.saved_dir = str(saved)

    items: list[WishlistItem] = []
    seconds: list[float] = []
    next_url: str | None = url
    blocked = False
    while next_url and report.pages < MAX_PAGES:
        if report.pages:
            sleep(random.uniform(*DELAY_RANGE))
        res = fetch(next_url)
        report.http_status = res.status
        seconds.append(res.seconds)
        if res.status != 200:
            blocked = True
            break
        if is_captcha(res.html):
            report.captcha = blocked = True
            break
        report.pages += 1
        (saved / f"page-{report.pages}.html").write_text(res.html, encoding="utf-8")
        page = parse_wishlist_page(res.html)
        report.list_title = report.list_title or page.title
        items.extend(page.items)
        next_url = page.next_url

    report.avg_seconds = sum(seconds) / len(seconds) if seconds else 0.0
    report.products = len(items)
    report.price_read = sum(i.price_ore is not None for i in items)
    report.prime_read = sum(i.prime is True for i in items)
    report.out_of_stock = sum(not i.in_stock for i in items)
    report.missing_price = [i.asin for i in items if i.in_stock and i.price_ore is None]
    report.product_pages_needed = sum(_needs_product_page(i) for i in items)
    report.items = [
        {"asin": i.asin, "title": i.title, "price_ore": i.price_ore, "prime": i.prime}
        for i in items
    ]
    if blocked or not items:
        report.exit_code = 2
    else:
        report.exit_code = 1 if report.product_pages_needed else 0
    (saved / "report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _pct(n: int, total: int) -> str:
    return f"{round(100 * n / total)} %" if total else "-"


def render_report(r: ProbeReport) -> str:
    http = f"{r.http_status} {'OK' if r.http_status == 200 else ''}".strip() if r.http_status else "ingen respons"
    lines = [
        f"List Watch – probe  {r.started}",
        f'Lista:        "{r.list_title or "?"}" ({r.list_id or "?"})',
        f"HTTP:         {http} ({r.pages} sidor, {r.avg_seconds:.1f} s/sida i snitt)",
        f"CAPTCHA:      {'JA' if r.captcha else 'nej'}",
        "",
        f"Produkter:    {r.products}",
        f"  Pris läst:        {r.price_read} / {r.products}  ({_pct(r.price_read, r.products)})",
        f"  Prime-status:     {r.prime_read} / {r.products}  ({_pct(r.prime_read, r.products)})",
        f"  Ej i lager:        {r.out_of_stock}",
        f"  Saknar pris:       {len(r.missing_price)}"
        + (f"  → {', '.join(r.missing_price)}" if r.missing_price else ""),
        "",
        f"Produktsidor behövs per runda:  ~{r.product_pages_needed}  (saknat pris eller Prime-status)",
        "",
        f"Sparat:  {r.saved_dir}/",
        "",
    ]
    verdict = {0: "ALLT LÄSBART", 1: "DELVIS OK", 2: "BLOCKERAD ELLER FEL"}[r.exit_code]
    lines.append(f"Resultat: {verdict} (avslutskod {r.exit_code})")
    return "\n".join(lines)
