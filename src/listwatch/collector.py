"""One collection execution (omgång): a run per list, measurements, events and alerts.

Rules (spec "Datainsamling" and docs/spec-logg.md):
- The list page is the price source. A product in several lists is counted as read in
  every list's run, but measured (price_point + events) only in the first.
- Events are written only on change; unchanged products just count as read.
- A list that says `saknar_pris` cannot overrule `ej_i_lager` decided by a product page.
- Prime is not tracked (decided 2026-10-27): product pages are fetched only for local
  ASIN watches, which read their product page every omgång. CAPTCHA or HTTP error on a
  product page stops product pages for the rest of the omgång; CAPTCHA, 503 or 202 there
  also counts as blocked.
- A refused list page (CAPTCHA, HTTP 503 or HTTP 202 = JS challenge, `source.BLOCKED_STATUSES`)
  stops the whole omgång (remaining lists get no run), and the omgång counts as blocked for
  the scheduler's backoff.
- After each list is stored its new measurements are judged by `alerts` and alerts are
  sent at once. senaste_larm_pris is set only when the notification was sent, so a failed
  send is retried next round.
- Ops notifications per list: a list that can't be read (not blocked) LIST_FAILED_ALERT
  omgångar in a row (0 items or HTTP 403/404/410 = `privat`: private, removed or empty;
  Amazon shows the same empty page for all three), and when it can be read again; more
  than PARSE_ERROR_SHARE (and at least PARSE_ERROR_MIN) of a list's items not understood (no ASIN or title, or a price
  lost this omgång, which a layout change does to every item at once), once until it
  drops back under.
- `progress`, if given, gets one line per list item, product page and alert as they happen.
"""
import sqlite3
import statistics
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import alerts
from .db import utc_iso
from .notify import Notifier, notifier_from_settings
from .parser import BASE_URL, STATUS_MISSING_PRICE, STATUS_OUT_OF_STOCK, STATUS_PRICE, WishlistItem
from .source import Source

SUSPICIOUS_SHARE = 0.10  # below 10 % of the median
CONFIRM_SHARE = 0.10  # a suspicious price is confirmed by a next price within ±10 %
MEDIAN_DAYS = 90
LIST_FAILED_ALERT = 2
PARSE_ERROR_SHARE = 0.20
PARSE_ERROR_MIN = 3  # a small list losing one or two prices is not a layout change
GONE_STATUSES = (403, 404, 410)

Clock = Callable[[], datetime]
Progress = Callable[[str], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Omgang:
    id: int
    notifier: Notifier
    say: Progress = lambda _line: None
    seen: set[str] = field(default_factory=set)
    product_pages_blocked: bool = False
    blocked: bool = False  # CAPTCHA, HTTP 503 or 202 anywhere: back off
    list_blocked: bool = False  # stop reading further lists


@dataclass
class CollectResult:
    omgang: int
    run_ids: list[int]
    blocked: bool

    def __iter__(self) -> Iterator[int]:
        return iter(self.run_ids)


class _Run:
    """A run row plus its counters; events are written as they happen."""

    def __init__(self, conn: sqlite3.Connection, omgang: int, typ: str, wl: sqlite3.Row, now: Clock):
        self.conn = conn
        self.now = now
        self.count: Counter[str] = Counter()
        self.captcha: str | None = None
        self.missing_price = 0
        self.read_failed = False  # the list (or every local watch) could not be read
        self.price_lost = 0  # items that had a price last time and have none now
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


def send_ops_notice(
    conn: sqlite3.Connection, notifier: Notifier, run_id: int | None, now: datetime, title: str, body: str
) -> None:
    """Send an ops notification and log it as a drift_notis event (with the error if it failed)."""
    error = notifier.send(title, body)
    conn.execute(
        "INSERT INTO event (run_id, tid, typ, text) VALUES (?, ?, 'drift_notis', ?)",
        (run_id, utc_iso(now), body if error is None else f"{body} Misslyckades: {error}"),
    )


def collect(
    conn: sqlite3.Connection,
    source: Source,
    typ: str = "schemalagd",
    now: Clock = _utcnow,
    notifier: Notifier | None = None,
    progress: Progress | None = None,
) -> CollectResult:
    """Read every list once and send alerts. Iterating the result gives the run ids."""
    omgang = _Omgang(
        id=conn.execute("SELECT coalesce(max(omgang), 0) + 1 FROM run").fetchone()[0],
        notifier=notifier or notifier_from_settings(conn),
    )
    if progress:
        omgang.say = progress
    run_ids = []
    for wl in conn.execute("SELECT * FROM wishlist ORDER BY id").fetchall():
        run = _Run(conn, omgang.id, typ, wl, now)
        omgang.say(f"{wl['namn']}:")
        if wl["typ"] == "lokal":
            _collect_local(conn, source, run, wl, omgang)
        else:
            _collect_list(conn, source, run, wl, omgang)
        run.finish(run.resultat())
        run_ids.append(run.id)
        if omgang.list_blocked:
            break
    return CollectResult(omgang=omgang.id, run_ids=run_ids, blocked=omgang.blocked)


def _collect_list(conn: sqlite3.Connection, source: Source, run: _Run, wl: sqlite3.Row, omgang: _Omgang) -> None:
    read = source.read_list(wl["url"])
    run.count["sidor"] = read.pages
    if read.retried_after:  # logged, but not an error unless the retry failed too
        run.event(
            "captcha" if read.retried_after == "CAPTCHA" else "http_fel",
            text=f"listsida: {read.retried_after}, nytt försök efter paus",
        )
    if read.error:
        run.read_failed = True
        blocked = read.blocked
        status = blocked or f"HTTP {read.http_status or 'inget svar'}"
        if read.error == "captcha":
            run.captcha = "listsida"
            run.error("captcha", text="listsida")
        elif read.error == "http":
            run.error("http_fel", text=f"listsida: {status}")
        else:
            run.error("parsningsfel", text="inga produkter hittades på listsidan")
        omgang.list_blocked = bool(blocked)
        omgang.blocked = omgang.blocked or omgang.list_blocked
        omgang.say(f"  kunde inte läsa listan ({read.error}, {status})")
        if blocked:  # has its own ops notification after 3 blocked omgångar
            conn.execute("UPDATE wishlist SET status = 'blockerad' WHERE id = ?", (wl["id"],))
            return
        gone = read.error == "parse" or read.http_status in GONE_STATUSES
        failed = wl["fel_i_rad"] + 1
        conn.execute(
            "UPDATE wishlist SET status = ?, fel_i_rad = ? WHERE id = ?", ("privat" if gone else "fel", failed, wl["id"])
        )
        if failed == LIST_FAILED_ALERT:
            reason = (
                "Amazon visar inga produkter: listan kan ha gjorts privat, tagits bort eller tömts."
                if gone else f"Fel vid läsning (HTTP {read.http_status or 'inget svar'})."
            )
            send_ops_notice(
                conn, omgang.notifier, run.id, run.now(), f"listwatch: listan {wl['namn']} går inte att läsa",
                f"Listan {wl['namn']} har inte gått att läsa {failed} omgångar i rad. {reason} Historiken finns kvar.",
            )
        return

    items = list({i.asin: i for i in read.items}.values())
    conn.execute("BEGIN")
    try:
        _apply_list(conn, run, wl, items, omgang)
        conn.execute(
            "UPDATE wishlist SET status = 'ok', senast_synkad = ?, fel_i_rad = 0 WHERE id = ?",
            (utc_iso(run.now()), wl["id"]),
        )
        if read.title and wl["namn"] == wl["list_id"]:  # added without a name: take Amazon's
            conn.execute("UPDATE wishlist SET namn = ? WHERE id = ?", (read.title, wl["id"]))
            conn.execute("UPDATE run SET list_namn = ? WHERE id = ?", (read.title, run.id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if wl["fel_i_rad"] >= LIST_FAILED_ALERT:
        name = read.title if wl["namn"] == wl["list_id"] and read.title else wl["namn"]
        send_ops_notice(
            conn, omgang.notifier, run.id, run.now(), f"listwatch: listan {name} går att läsa igen",
            f"Listan {name} gick att läsa igen efter {wl['fel_i_rad']} misslyckade omgångar.",
        )
    _check_parse_errors(conn, run, wl, read.unparsed, items, omgang)
    _check_alerts(conn, run, omgang)


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
        notes = []
        if item.asin not in members or members[item.asin] is not None:
            run.count["nya"] += 1
            run.event("ny_produkt", item.asin)
            notes.append("ny")
        if item.asin not in omgang.seen:
            omgang.seen.add(item.asin)
            if item.status == STATUS_MISSING_PRICE:
                prev = conn.execute("SELECT senaste_status FROM product WHERE asin = ?", (item.asin,)).fetchone()[0]
                run.price_lost += prev == STATUS_PRICE
            notes.append(_measure(conn, run, item.asin, item.status, item.price_ore, "lista"))
        else:
            notes.append("redan mätt i en tidigare lista")
        omgang.say(_line(item.asin, item.status, item.price_ore, item.title, notes))

    read = {i.asin for i in items}
    for asin, borttagen in members.items():
        if borttagen is None and asin not in read:
            conn.execute(
                "UPDATE wishlist_product SET borttagen = ? WHERE wishlist_id = ? AND asin = ?",
                (now, wl["id"], asin),
            )
            run.count["borttagna"] += 1
            run.event("borttagen_produkt", asin)
            omgang.say(f"  {asin}  borttagen från listan")
    conn.execute(
        """UPDATE product SET aktiv = 0 WHERE aktiv = 1 AND NOT EXISTS (
               SELECT 1 FROM wishlist_product wp WHERE wp.asin = product.asin AND wp.borttagen IS NULL)"""
    )


def _check_parse_errors(
    conn: sqlite3.Connection, run: _Run, wl: sqlite3.Row, unparsed: int, items: list[WishlistItem], omgang: _Omgang
) -> None:
    total = len(items) + unparsed
    bad = unparsed + sum(1 for i in items if not i.title) + run.price_lost
    if bad <= PARSE_ERROR_SHARE * total or bad < PARSE_ERROR_MIN:
        if wl["parserfel"]:
            conn.execute("UPDATE wishlist SET parserfel = 0 WHERE id = ?", (wl["id"],))
        return
    detail = f"{bad} av {total} produkter kunde inte tolkas"
    parts = [f"{n} {what}" for n, what in (
        (unparsed, "utan ASIN"), (sum(1 for i in items if not i.title), "utan titel"),
        (run.price_lost, "tappade sitt pris"),
    ) if n]
    detail += f" ({', '.join(parts)})"
    omgang.say(f"  parserfel: {detail}")
    if wl["parserfel"]:
        return
    run.error("parsningsfel", text=detail)
    conn.execute("UPDATE wishlist SET parserfel = 1 WHERE id = ?", (wl["id"],))
    send_ops_notice(
        conn, omgang.notifier, run.id, run.now(), f"listwatch: parserfel på listan {wl['namn']}",
        f"{detail} på listan {wl['namn']}. Amazon kan ha ändrat sidlayouten; parsern behöver ses över.",
    )


def _line(asin: str, status: str, price: int | None, title: str | None, notes: list[str | None]) -> str:
    shown = alerts.kr(price) if status == STATUS_PRICE and price is not None else status.replace("_", " ")
    line = f"  {asin}  {shown:>14}  {(title or '')[:50]}"
    notes = [n for n in notes if n]
    return f"{line}  [{', '.join(notes)}]" if notes else line


def _suspicious(conn: sqlite3.Connection, asin: str, price: int, now: datetime) -> bool:
    """Below 10 % of the median, unless the previous measurement already had about this price."""
    prev = conn.execute(
        "SELECT pris_ore FROM price_point WHERE asin = ? AND status = 'pris' ORDER BY tidpunkt DESC, id DESC LIMIT 1",
        (asin,),
    ).fetchone()
    if prev is not None and abs(prev[0] - price) <= CONFIRM_SHARE * price:
        return False
    since = utc_iso(now - timedelta(days=MEDIAN_DAYS))
    prices = [
        r[0]
        for r in conn.execute(
            "SELECT pris_ore FROM price_point WHERE asin = ? AND status = 'pris' AND misstankt = 0 AND tidpunkt >= ?",
            (asin, since),
        )
    ]
    return bool(prices) and price < SUSPICIOUS_SHARE * statistics.median(prices)


def _measure(conn: sqlite3.Connection, run: _Run, asin: str, status: str, price: int | None, kalla: str) -> str | None:
    """Store one measurement and write events for what changed. Returns a short note on the change."""
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
    notes = ["misstänkt pris"] if misstankt else []
    if status == STATUS_MISSING_PRICE and prev == STATUS_OUT_OF_STOCK:
        new = prev  # the list can't tell; keep what a product page decided
    if new == STATUS_PRICE:
        changed = last_price is not None and last_price != price
        if changed:
            run.count["prisandrade"] += 1
        if prev == STATUS_OUT_OF_STOCK:
            run.event("åter_i_lager", asin, fore=last_price, efter=price)
            notes.append("åter i lager")
        elif changed:
            run.event("pris_ändrat", asin, fore=last_price, efter=price)
            notes.append(f"var {alerts.kr(last_price)}")
    elif new != prev:
        run.event(new, asin, fore=prev)
        if prev is not None:
            notes.append(f"nu {new.replace('_', ' ')}")
    conn.execute(
        "UPDATE product SET senaste_status = ?, senaste_pris_ore = ?, senast_last = ? WHERE asin = ?",
        (new, price if price is not None else last_price, utc_iso(now), asin),
    )
    return ", ".join(notes) or None


def _read_product(source: Source, run: _Run, asin: str, omgang: _Omgang):
    """Fetch one product page (local watches). None on CAPTCHA/HTTP error (202/503 = blocked)."""
    res = source.read_product(asin)
    run.count["produktsidor"] += 1
    if res.captcha or res.page is None:
        omgang.product_pages_blocked = True
        omgang.blocked = omgang.blocked or bool(res.blocked)
        if res.captcha:
            run.captcha = run.captcha or "produktsida"
            run.error("captcha", asin, text="produktsida")
            omgang.say(f"  {asin}  produktsida: CAPTCHA, inga fler produktsidor denna omgång")
        else:
            status = res.blocked or f"HTTP {res.http_status or 'inget svar'}"
            run.error("http_fel", asin, text=f"produktsida: {status}")
            omgang.say(f"  {asin}  produktsida: {status}, inga fler produktsidor denna omgång")
        return None
    return res.page


def _check_alerts(conn: sqlite3.Connection, run: _Run, omgang: _Omgang) -> None:
    points = conn.execute(
        "SELECT id, asin FROM price_point WHERE run_id = ? AND status = 'pris' AND misstankt = 0 ORDER BY id",
        (run.id,),
    ).fetchall()
    for point in points:
        asin = point["asin"]
        d = alerts.evaluate(conn, point["id"], run.now())
        if d is None:
            continue
        if d.reset:
            conn.execute("UPDATE alert_rule SET senaste_larm_pris = NULL WHERE asin = ?", (asin,))
        if not d.notify:
            continue
        prod = conn.execute("SELECT titel, url FROM product WHERE asin = ?", (asin,)).fetchone()
        title, body = alerts.message(prod["titel"] or asin, prod["url"], d)
        error = omgang.notifier.send(title, body)
        if error:
            run.count["larm_misslyckade"] += 1
            run.event("larm_misslyckades", asin, efter=d.price_ore, text=error)
            omgang.say(f"  {asin}  LARM misslyckades: {error}")
            continue
        run.count["larm_skickade"] += 1
        run.event("larm_skickat", asin, efter=d.price_ore, text=body)
        omgang.say(f"  {asin}  LARM skickat: {alerts.kr(d.price_ore)}")
        if d.threshold_alert:
            conn.execute(
                """INSERT INTO alert_rule (asin, senaste_larm_pris, senaste_larm_tid) VALUES (?, ?, ?)
                   ON CONFLICT (asin) DO UPDATE SET
                       senaste_larm_pris = excluded.senaste_larm_pris,
                       senaste_larm_tid = excluded.senaste_larm_tid""",
                (asin, d.price_ore, utc_iso(run.now())),
            )


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
        page = _read_product(source, run, asin, omgang)
        if page is None:
            break
        run.count["lasta"] += 1
        if page.status == STATUS_MISSING_PRICE:
            run.missing_price += 1
        omgang.seen.add(asin)
        if page.title:
            conn.execute("UPDATE product SET titel = ? WHERE asin = ? AND titel = ''", (page.title, asin))
        note = _measure(conn, run, asin, page.status, page.price_ore, "produktsida")
        title = conn.execute("SELECT titel FROM product WHERE asin = ?", (asin,)).fetchone()[0]
        omgang.say(_line(asin, page.status, page.price_ore, title, [note]))
    run.read_failed = bool(asins) and run.count["lasta"] == 0
    _check_alerts(conn, run, omgang)
    conn.execute(
        "UPDATE wishlist SET status = ?, senast_synkad = ? WHERE id = ?",
        ("blockerad" if omgang.product_pages_blocked else "ok", utc_iso(run.now()), wl["id"]),
    )
