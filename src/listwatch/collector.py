"""One collection execution (omgång): a run per list, measurements, events and Prime refresh.

Rules (spec "Datainsamling" and docs/spec-logg.md):
- The list page is the price source. A product in several lists is counted as read in
  every list's run, but measured (price_point + events) only in the first.
- Events are written only on change; unchanged products just count as read.
- A list that says `saknar_pris` cannot overrule `ej_i_lager` decided by a product page.
- Product pages are fetched for stale Prime caches (oldest first, capped per omgång) and
  always for local ASIN watches. CAPTCHA or HTTP error on a product page stops product
  pages for the rest of the omgång.
"""
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .db import setting_int, utc_iso
from .parser import BASE_URL, STATUS_MISSING_PRICE, STATUS_OUT_OF_STOCK, STATUS_PRICE, WishlistItem
from .source import Source

PRIME = {True: "prime", False: "inte_prime", None: "okänd"}
SUSPICIOUS_SHARE = 0.10  # below 10 % of the median
MEDIAN_DAYS = 90

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Omgang:
    id: int
    product_pages_left: int  # background refresh budget
    prime_ttl: timedelta
    seen: set[str] = field(default_factory=set)
    product_pages_blocked: bool = False


class _Run:
    """A run row plus its counters; events are written as they happen."""

    def __init__(self, conn: sqlite3.Connection, omgang: int, typ: str, wl: sqlite3.Row, now: Clock):
        self.conn = conn
        self.now = now
        self.count: Counter[str] = Counter()
        self.captcha: str | None = None
        self.missing_price = 0
        self.read_failed = False  # the list (or every local watch) could not be read
        self.id = conn.execute(
            "INSERT INTO run (omgang, typ, wishlist_id, list_namn, list_id, start) VALUES (?, ?, ?, ?, ?, ?)",
            (omgang, typ, wl["id"], wl["namn"], wl["list_id"], utc_iso(now())),
        ).lastrowid

    def event(self, typ: str, asin: str | None = None, fore=None, efter=None, text: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO event (run_id, tid, typ, asin, fore, efter, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.id, utc_iso(self.now()), typ, asin,
                None if fore is None else str(fore),
                None if efter is None else str(efter),
                text,
            ),
        )

    def error(self, typ: str, asin: str | None = None, text: str | None = None) -> None:
        self.count["fel"] += 1
        self.event(typ, asin=asin, text=text)

    def finish(self, resultat: str) -> None:
        c = self.count
        self.conn.execute(
            """UPDATE run SET slut = ?, sidor = ?, lasta = ?, prisandrade = ?, nya = ?, borttagna = ?,
                   produktsidor = ?, prime_andrade = ?, larm_skickade = ?, larm_misslyckade = ?,
                   fel = ?, captcha = ?, resultat = ?
               WHERE id = ?""",
            (
                utc_iso(self.now()), c["sidor"], c["lasta"], c["prisandrade"], c["nya"], c["borttagna"],
                c["produktsidor"], c["prime_andrade"], c["larm_skickade"], c["larm_misslyckade"],
                c["fel"], self.captcha, resultat, self.id,
            ),
        )

    def resultat(self) -> str:
        if self.read_failed:
            return "fel"
        if self.missing_price or self.count["fel"] or self.count["larm_misslyckade"]:
            return "delvis"
        return "ok"


def collect(conn: sqlite3.Connection, source: Source, typ: str = "schemalagd", now: Clock = _utcnow) -> list[int]:
    """Read every list once. Returns the run ids, one per list."""
    omgang = _Omgang(
        id=conn.execute("SELECT coalesce(max(omgang), 0) + 1 FROM run").fetchone()[0],
        product_pages_left=setting_int(conn, "max_product_pages_per_round"),
        prime_ttl=timedelta(seconds=setting_int(conn, "prime_ttl")),
    )
    run_ids = []
    for wl in conn.execute("SELECT * FROM wishlist ORDER BY id").fetchall():
        run = _Run(conn, omgang.id, typ, wl, now)
        if wl["typ"] == "lokal":
            _collect_local(conn, source, run, wl, omgang)
        else:
            _collect_list(conn, source, run, wl, omgang)
        run.finish(run.resultat())
        run_ids.append(run.id)
    return run_ids


def _collect_list(conn: sqlite3.Connection, source: Source, run: _Run, wl: sqlite3.Row, omgang: _Omgang) -> None:
    read = source.read_list(wl["url"])
    run.count["sidor"] = read.pages
    if read.error:
        run.read_failed = True
        if read.error == "captcha":
            run.captcha = "listsida"
            run.error("captcha", text="listsida")
        elif read.error == "http":
            run.error("http_fel", text=f"listsida: HTTP {read.http_status or 'inget svar'}")
        else:
            run.error("parsningsfel", text="inga produkter hittades på listsidan")
        status = "blockerad" if read.captcha or read.http_status == 503 else "fel"
        conn.execute("UPDATE wishlist SET status = ? WHERE id = ?", (status, wl["id"]))
        return

    items = list({i.asin: i for i in read.items}.values())
    conn.execute("BEGIN")
    try:
        _apply_list(conn, run, wl, items, omgang)
        conn.execute(
            "UPDATE wishlist SET status = 'ok', senast_synkad = ? WHERE id = ?", (utc_iso(run.now()), wl["id"])
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    _refresh_prime(conn, source, run, {i.asin for i in items}, omgang)


def _apply_list(conn: sqlite3.Connection, run: _Run, wl: sqlite3.Row, items: list[WishlistItem], omgang: _Omgang) -> None:
    now = utc_iso(run.now())
    members = {
        r["asin"]: r["borttagen"]
        for r in conn.execute("SELECT asin, borttagen FROM wishlist_product WHERE wishlist_id = ?", (wl["id"],))
    }
    for item in items:
        run.count["lasta"] += 1
        if item.status == STATUS_MISSING_PRICE:
            run.missing_price += 1
        conn.execute(
            """INSERT INTO product (asin, titel, bild_url, url, forsta_sedd) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (asin) DO UPDATE SET
                   titel = coalesce(nullif(excluded.titel, ''), titel),
                   bild_url = coalesce(excluded.bild_url, bild_url),
                   aktiv = 1""",
            (item.asin, item.title, item.image_url, f"{BASE_URL}/dp/{item.asin}", now),
        )
        if item.asin not in members:
            conn.execute(
                "INSERT INTO wishlist_product (wishlist_id, asin, tillagd) VALUES (?, ?, ?)",
                (wl["id"], item.asin, now),
            )
        elif members[item.asin] is not None:
            conn.execute(
                "UPDATE wishlist_product SET borttagen = NULL, tillagd = ? WHERE wishlist_id = ? AND asin = ?",
                (now, wl["id"], item.asin),
            )
        if item.asin not in members or members[item.asin] is not None:
            run.count["nya"] += 1
            run.event("ny_produkt", item.asin)
        if item.asin not in omgang.seen:
            omgang.seen.add(item.asin)
            _measure(conn, run, item.asin, item.status, item.price_ore, "lista")

    read = {i.asin for i in items}
    for asin, borttagen in members.items():
        if borttagen is None and asin not in read:
            conn.execute(
                "UPDATE wishlist_product SET borttagen = ? WHERE wishlist_id = ? AND asin = ?",
                (now, wl["id"], asin),
            )
            run.count["borttagna"] += 1
            run.event("borttagen_produkt", asin)
    conn.execute(
        """UPDATE product SET aktiv = 0 WHERE aktiv = 1 AND NOT EXISTS (
               SELECT 1 FROM wishlist_product wp WHERE wp.asin = product.asin AND wp.borttagen IS NULL)"""
    )


def _suspicious(conn: sqlite3.Connection, asin: str, price: int, now: datetime) -> bool:
    since = utc_iso(now - timedelta(days=MEDIAN_DAYS))
    prices = [
        r[0]
        for r in conn.execute(
            "SELECT pris_ore FROM price_point WHERE asin = ? AND status = 'pris' AND misstankt = 0 AND tidpunkt >= ?",
            (asin, since),
        )
    ]
    return bool(prices) and price < SUSPICIOUS_SHARE * statistics.median(prices)


def _measure(conn: sqlite3.Connection, run: _Run, asin: str, status: str, price: int | None, kalla: str) -> None:
    """Store one measurement and write events for what changed."""
    now = run.now()
    prod = conn.execute(
        "SELECT senaste_status, senaste_pris_ore, prime_status FROM product WHERE asin = ?", (asin,)
    ).fetchone()
    price = price if status == STATUS_PRICE else None
    misstankt = price is not None and _suspicious(conn, asin, price, now)
    conn.execute(
        """INSERT INTO price_point (asin, tidpunkt, status, pris_ore, prime, kalla, misstankt, run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (asin, utc_iso(now), status, price, prod["prime_status"], kalla, int(misstankt), run.id),
    )
    prev, last_price = prod["senaste_status"], prod["senaste_pris_ore"]
    new = status
    if status == STATUS_MISSING_PRICE and prev == STATUS_OUT_OF_STOCK:
        new = prev  # the list can't tell; keep what a product page decided
    if new == STATUS_PRICE:
        changed = last_price is not None and last_price != price
        if changed:
            run.count["prisandrade"] += 1
        if prev == STATUS_OUT_OF_STOCK:
            run.event("åter_i_lager", asin, fore=last_price, efter=price)
        elif changed:
            run.event("pris_ändrat", asin, fore=last_price, efter=price)
    elif new != prev:
        run.event(new, asin, fore=prev)
    conn.execute(
        "UPDATE product SET senaste_status = ?, senaste_pris_ore = ?, senast_last = ? WHERE asin = ?",
        (new, price if price is not None else last_price, utc_iso(now), asin),
    )


def _read_product(conn: sqlite3.Connection, source: Source, run: _Run, asin: str, reason: str | None, omgang: _Omgang):
    """Fetch one product page and update the Prime cache. None on CAPTCHA/HTTP error.

    `reason` ('bakgrund'/'larmkontroll') is logged as a produktsida_hämtad event; local
    watches pass None since they read their product page every round.
    """
    res = source.read_product(asin)
    run.count["produktsidor"] += 1
    if res.captcha or res.page is None:
        omgang.product_pages_blocked = True
        if res.captcha:
            run.captcha = run.captcha or "produktsida"
            run.error("captcha", asin, text="produktsida")
        else:
            run.error("http_fel", asin, text=f"produktsida: HTTP {res.http_status or 'inget svar'}")
        return None
    if reason:
        run.event("produktsida_hämtad", asin, text=reason)
    old = conn.execute("SELECT prime_status FROM product WHERE asin = ?", (asin,)).fetchone()[0]
    new = PRIME[res.page.prime]
    if new != old:
        run.count["prime_andrade"] += 1
        run.event("prime_ändrad", asin, fore=old, efter=new)
    conn.execute(
        "UPDATE product SET prime_status = ?, prime_kontrollerad = ? WHERE asin = ?",
        (new, utc_iso(run.now()), asin),
    )
    return res.page


def _refresh_prime(conn: sqlite3.Connection, source: Source, run: _Run, asins: set[str], omgang: _Omgang) -> None:
    if omgang.product_pages_blocked or omgang.product_pages_left <= 0:
        return
    cutoff = utc_iso(run.now() - omgang.prime_ttl)
    stale = [
        r["asin"]
        for r in conn.execute(
            """SELECT asin FROM product
               WHERE aktiv = 1 AND (prime_kontrollerad IS NULL OR prime_kontrollerad < ?)
               ORDER BY prime_kontrollerad IS NOT NULL, prime_kontrollerad, asin""",
            (cutoff,),
        )
        if r["asin"] in asins
    ]
    for asin in stale[: omgang.product_pages_left]:
        if omgang.product_pages_blocked:
            break
        omgang.product_pages_left -= 1
        page = _read_product(conn, source, run, asin, "bakgrund", omgang)
        if page is None:
            continue
        status = conn.execute("SELECT senaste_status FROM product WHERE asin = ?", (asin,)).fetchone()[0]
        if page.status == STATUS_OUT_OF_STOCK and status == STATUS_MISSING_PRICE:
            _measure(conn, run, asin, STATUS_OUT_OF_STOCK, None, "produktsida")


def _collect_local(conn: sqlite3.Connection, source: Source, run: _Run, wl: sqlite3.Row, omgang: _Omgang) -> None:
    """Local ASIN watches are always read from the product page."""
    asins = [
        r["asin"]
        for r in conn.execute(
            "SELECT asin FROM wishlist_product WHERE wishlist_id = ? AND borttagen IS NULL ORDER BY asin", (wl["id"],)
        )
    ]
    for asin in asins:
        if asin in omgang.seen:
            run.count["lasta"] += 1
            continue
        if omgang.product_pages_blocked:
            break
        page = _read_product(conn, source, run, asin, None, omgang)
        if page is None:
            break
        run.count["lasta"] += 1
        if page.status == STATUS_MISSING_PRICE:
            run.missing_price += 1
        omgang.seen.add(asin)
        _measure(conn, run, asin, page.status, page.price_ore, "produktsida")
    run.read_failed = bool(asins) and run.count["lasta"] == 0
    conn.execute(
        "UPDATE wishlist SET status = ?, senast_synkad = ? WHERE id = ?",
        ("blockerad" if omgang.product_pages_blocked else "ok", utc_iso(run.now()), wl["id"]),
    )
