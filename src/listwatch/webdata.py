"""Read models for the web UI: what each page shows, computed from the database.

Reference, rule and levels come from `alerts`, so the GUI shows exactly what the alert
engine would use. Prices stay integer öre here; templates format them.
"""
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import alerts
from .db import NOTIFICATION_EVENTS, utc_iso

TREND_DAYS = 7
PERIODS = {"30d": 30, "90d": 90, "1ar": 365, "allt": None}


@dataclass
class Level:
    """The alert level(s) a price is judged against."""

    rule: alerts.Rule
    reference: alerts.Reference | None
    percent_level_ore: int | None = None  # ref × (1 − pct/100), when percent alerts can fire
    waiting_for_history: bool = False  # percent rule set but < 7 days of reference data

    @property
    def any_condition(self) -> bool:
        return self.rule.target_ore is not None or self.rule.percent is not None


def level_for(conn: sqlite3.Connection, asin: str, now: datetime) -> Level:
    rule = alerts.rule_for(conn, asin)
    ref = alerts.reference(conn, asin, now)
    lv = Level(rule=rule, reference=ref)
    if rule.percent is not None:
        if ref is not None and ref.days >= alerts.MIN_PERCENT_DAYS:
            lv.percent_level_ore = round(ref.median_ore * (1 - rule.percent / 100))
        else:
            lv.waiting_for_history = True
    return lv


@dataclass
class OverviewRow:
    asin: str
    titel: str
    bild_url: str | None
    url: str | None
    status: str | None
    pris_ore: int | None  # current price, None unless status is pris
    level: Level
    rabatt: float | None  # percent below the reference (negative = above)
    trend: str | None  # 'ned', 'upp', 'lika' vs. TREND_DAYS ago, None = no earlier price
    prime: str
    lists: list[str] = field(default_factory=list)

    @property
    def larm_status(self) -> str:
        rule = self.level.rule
        if not rule.active:
            return "av"
        if rule.last_alert_ore is not None:
            return "larmat"
        if not self.level.any_condition:
            return "ingen_regel"
        if self.level.waiting_for_history and rule.target_ore is None:
            return "vantar"
        return "bevakas"


def _trend(conn: sqlite3.Connection, asin: str, price: int | None, now: datetime) -> str | None:
    if price is None:
        return None
    row = conn.execute(
        """SELECT pris_ore FROM price_point WHERE asin = ? AND status = 'pris' AND misstankt = 0
             AND tidpunkt <= ? ORDER BY tidpunkt DESC, id DESC LIMIT 1""",
        (asin, utc_iso(now - timedelta(days=TREND_DAYS))),
    ).fetchone()
    if row is None:  # younger than a week: compare with the first price seen
        row = conn.execute(
            """SELECT pris_ore FROM price_point WHERE asin = ? AND status = 'pris' AND misstankt = 0
               ORDER BY tidpunkt, id LIMIT 1""",
            (asin,),
        ).fetchone()
    if row is None:
        return None
    return "ned" if price < row[0] else "upp" if price > row[0] else "lika"


_OVERVIEW_SQL = """SELECT p.*, group_concat(w.namn, ', ') AS listor FROM product p
             JOIN wishlist_product wp ON wp.asin = p.asin AND wp.borttagen IS NULL
             JOIN wishlist w ON w.id = wp.wishlist_id
             WHERE p.aktiv = 1"""


def _row(conn: sqlite3.Connection, p: sqlite3.Row, now: datetime) -> OverviewRow:
    price = p["senaste_pris_ore"] if p["senaste_status"] == "pris" else None
    lv = level_for(conn, p["asin"], now)
    ref = lv.reference.median_ore if lv.reference else None
    return OverviewRow(
        asin=p["asin"], titel=p["titel"] or p["asin"], bild_url=p["bild_url"], url=p["url"],
        status=p["senaste_status"], pris_ore=price, level=lv,
        rabatt=(1 - price / ref) * 100 if price is not None and ref else None,
        trend=_trend(conn, p["asin"], price, now), prime=p["prime_status"],
        lists=(p["listor"] or "").split(", "),
    )


def overview_row(conn: sqlite3.Connection, asin: str, now: datetime) -> OverviewRow | None:
    p = conn.execute(_OVERVIEW_SQL + " AND p.asin = ? GROUP BY p.asin", (asin,)).fetchone()
    return _row(conn, p, now) if p else None


def overview(conn: sqlite3.Connection, now: datetime, lista: int | None = None, sort: str = "rabatt") -> list[OverviewRow]:
    sql = _OVERVIEW_SQL
    params: tuple = ()
    if lista is not None:
        sql += " AND p.asin IN (SELECT asin FROM wishlist_product WHERE wishlist_id = ? AND borttagen IS NULL)"
        params = (lista,)
    rows = [_row(conn, p, now) for p in conn.execute(sql + " GROUP BY p.asin", params).fetchall()]
    if sort == "namn":
        rows.sort(key=lambda r: r.titel.lower())
    elif sort == "pris":
        rows.sort(key=lambda r: (r.pris_ore is None, r.pris_ore or 0))
    else:  # biggest discount first, unpriced last
        rows.sort(key=lambda r: (r.rabatt is None, -(r.rabatt or 0), r.titel.lower()))
    return rows


@dataclass
class Stats:
    lagsta_ore: int
    hogsta_ore: int
    median_ore: int


@dataclass
class ProductView:
    product: sqlite3.Row
    rule_row: sqlite3.Row | None
    level: Level
    lists: list[sqlite3.Row]
    inherited_percent: int | None  # what "ärv standard" gives: lowest active list default
    points: list[sqlite3.Row]  # within the period, oldest first
    stats: Stats | None
    local_watch: bool


def product_view(conn: sqlite3.Connection, asin: str, now: datetime, period: str = "90d") -> ProductView | None:
    product = conn.execute("SELECT * FROM product WHERE asin = ?", (asin,)).fetchone()
    if product is None:
        return None
    days = PERIODS.get(period, 90)
    since = utc_iso(now - timedelta(days=days)) if days else ""
    points = conn.execute(
        "SELECT * FROM price_point WHERE asin = ? AND tidpunkt >= ? ORDER BY tidpunkt, id", (asin, since)
    ).fetchall()
    valid = [p["pris_ore"] for p in points if p["status"] == "pris" and not p["misstankt"]]
    lists = conn.execute(
        """SELECT w.* FROM wishlist w JOIN wishlist_product wp ON wp.wishlist_id = w.id
           WHERE wp.asin = ? AND wp.borttagen IS NULL ORDER BY w.id""",
        (asin,),
    ).fetchall()
    defaults = [w["standard_procent"] for w in lists if w["standard_aktiv"]]
    return ProductView(
        product=product,
        rule_row=conn.execute("SELECT * FROM alert_rule WHERE asin = ?", (asin,)).fetchone(),
        level=level_for(conn, asin, now),
        lists=lists,
        inherited_percent=min(defaults) if defaults else None,
        points=points,
        stats=Stats(min(valid), max(valid), round(statistics.median(valid))) if valid else None,
        local_watch=any(w["typ"] == "lokal" for w in lists),
    )


def chart_data(view: ProductView) -> dict:
    """Chart.js series: price in kr (null = gap: out of stock / no price), reference and level lines."""
    xs = [p["tidpunkt"] for p in view.points]
    price = [p["pris_ore"] / 100 if p["status"] == "pris" and not p["misstankt"] else None for p in view.points]
    lv = view.level

    def flat(ore: int | None) -> list | None:
        return [ore / 100] * len(xs) if ore is not None and xs else None

    return {
        "x": xs,
        "pris": price,
        "referens": flat(lv.reference.median_ore if lv.reference else None),
        "mal": flat(lv.rule.target_ore),
        "procentniva": flat(lv.percent_level_ore),
    }


def lists_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT w.*,
                  (SELECT count(*) FROM wishlist_product wp
                   WHERE wp.wishlist_id = w.id AND wp.borttagen IS NULL) AS produkter,
                  (SELECT resultat FROM run r WHERE r.wishlist_id = w.id AND r.resultat IS NOT NULL
                   ORDER BY r.id DESC LIMIT 1) AS senaste_resultat
           FROM wishlist w ORDER BY w.typ, w.id"""
    ).fetchall()


def runs(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def events(
    conn: sqlite3.Connection, run_id: int | None = None, typ: str | None = None, asin: str | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    where, params = [], []
    for column, value in (("e.run_id", run_id), ("e.typ", typ), ("e.asin", asin)):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    sql = "SELECT e.*, p.titel FROM event e LEFT JOIN product p ON p.asin = e.asin"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql + " ORDER BY e.id DESC LIMIT ?", (*params, limit)).fetchall()


def notification_log(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    marks = ", ".join("?" * len(NOTIFICATION_EVENTS))
    return conn.execute(
        f"""SELECT e.*, p.titel FROM event e LEFT JOIN product p ON p.asin = e.asin
            WHERE e.typ IN ({marks}) ORDER BY e.id DESC LIMIT ?""",
        (*NOTIFICATION_EVENTS, limit),
    ).fetchall()
