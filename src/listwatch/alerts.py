"""Alert rules (spec "Larmlogik"): which measurements should notify, and the message text.

Pure decisions over the database; the collector does Prime checks, sending and bookkeeping.
- Reference = median of valid (pris, not suspicious) Prime measurements in the last 90 days,
  excluding the measurement being judged. Percent alerts need ≥ 7 days of that data.
- Fixed and percent conditions: any active one met = under the level.
- Suppression: alert on the first crossing, then only on a further ≥ 5 % drop below
  senaste_larm_pris; going above the level resets it.
- All-time low (opt-in): below every earlier valid price, after ≥ 30 days of history.
"""
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .db import utc_iso

REFERENCE_DAYS = 90
MIN_PERCENT_DAYS = 7
MIN_ALL_TIME_LOW_DAYS = 30
FURTHER_DROP = 0.05


@dataclass
class Rule:
    active: bool
    target_ore: int | None
    percent: int | None
    all_time_low: bool
    last_alert_ore: int | None


@dataclass
class Reference:
    median_ore: int
    days: int


@dataclass
class Decision:
    price_ore: int
    rule: Rule
    reference: Reference | None
    under_target: bool = False
    under_percent: bool = False
    further_drop: bool = False  # re-alert while suppressed
    all_time_low: bool = False
    previous_low_ore: int | None = None
    reset: bool = False  # back above the level: clear senaste_larm_pris
    reasons: list[str] = field(default_factory=list)

    @property
    def under_level(self) -> bool:
        return self.under_target or self.under_percent

    @property
    def threshold_alert(self) -> bool:
        """An alert that sets senaste_larm_pris."""
        return self.under_level and (self.rule.last_alert_ore is None or self.further_drop)

    @property
    def notify(self) -> bool:
        return self.threshold_alert or self.all_time_low


def _days(since: str, now: datetime) -> int:
    return (now - datetime.fromisoformat(since.replace("Z", "+00:00"))).days


def rule_for(conn: sqlite3.Connection, asin: str) -> Rule:
    row = conn.execute("SELECT * FROM alert_rule WHERE asin = ?", (asin,)).fetchone()
    if row is None or row["arver_standard"]:
        # Lowest default among the active lists that contain the product
        percent = conn.execute(
            """SELECT min(w.standard_procent) FROM wishlist w
               JOIN wishlist_product wp ON wp.wishlist_id = w.id
               WHERE wp.asin = ? AND wp.borttagen IS NULL AND w.standard_aktiv = 1""",
            (asin,),
        ).fetchone()[0]
    else:
        percent = row["mal_procent"]
    if row is None:
        return Rule(active=True, target_ore=None, percent=percent, all_time_low=False, last_alert_ore=None)
    return Rule(
        active=bool(row["larm_aktivt"]),
        target_ore=row["mal_pris_ore"],
        percent=percent,
        all_time_low=bool(row["lagsta_nagonsin_notis"]),
        last_alert_ore=row["senaste_larm_pris"],
    )


def reference(conn: sqlite3.Connection, asin: str, now: datetime, exclude_id: int | None = None) -> Reference | None:
    rows = conn.execute(
        """SELECT pris_ore, tidpunkt FROM price_point
           WHERE asin = ? AND status = 'pris' AND misstankt = 0 AND prime = 'prime'
             AND tidpunkt >= ? AND tidpunkt <= ? AND id IS NOT ?""",
        (asin, utc_iso(now - timedelta(days=REFERENCE_DAYS)), utc_iso(now), exclude_id),
    ).fetchall()
    if not rows:
        return None
    oldest = min(r["tidpunkt"] for r in rows)
    return Reference(median_ore=round(statistics.median(r["pris_ore"] for r in rows)), days=_days(oldest, now))


def evaluate(conn: sqlite3.Connection, point_id: int, now: datetime) -> Decision | None:
    """Judge one stored measurement. None if it can't alert at all (no price, suspicious, rule off)."""
    point = conn.execute("SELECT * FROM price_point WHERE id = ?", (point_id,)).fetchone()
    if point is None or point["status"] != "pris" or point["misstankt"]:
        return None
    asin, price = point["asin"], point["pris_ore"]
    rule = rule_for(conn, asin)
    if not rule.active:
        return None
    ref = reference(conn, asin, now, exclude_id=point_id)
    d = Decision(price_ore=price, rule=rule, reference=ref)

    if rule.target_ore is not None and price <= rule.target_ore:
        d.under_target = True
        d.reasons.append("mål")
    if rule.percent is not None and ref is not None and ref.days >= MIN_PERCENT_DAYS:
        if price <= ref.median_ore * (1 - rule.percent / 100):
            d.under_percent = True
            d.reasons.append("procent")
    if d.under_level and rule.last_alert_ore is not None:
        d.further_drop = price <= rule.last_alert_ore * (1 - FURTHER_DROP)
    if not d.under_level and rule.last_alert_ore is not None:
        d.reset = True

    if rule.all_time_low:
        hist = conn.execute(
            """SELECT min(pris_ore), min(tidpunkt) FROM price_point
               WHERE asin = ? AND status = 'pris' AND misstankt = 0 AND id != ?""",
            (asin, point_id),
        ).fetchone()
        if hist[0] is not None and _days(hist[1], now) >= MIN_ALL_TIME_LOW_DAYS and price < hist[0]:
            d.all_time_low = True
            d.previous_low_ore = hist[0]
    return d


def kr(ore: int) -> str:
    """'2 990 kr' or '2 990,50 kr'."""
    kronor, rest = divmod(ore, 100)
    whole = f"{kronor:,}".replace(",", " ")
    return f"{whole} kr" if rest == 0 else f"{whole},{rest:02d} kr"


def message(title: str, url: str, d: Decision) -> tuple[str, str]:
    """(notification title, body) as in the spec's example."""
    head = f"{title}: {kr(d.price_ore)}"
    if d.reference is not None and d.reference.median_ore > 0:
        pct = round(100 * (d.reference.median_ore - d.price_ore) / d.reference.median_ore)
        sign = "−" if pct >= 0 else "+"
        head += f" ({sign}{abs(pct)} % mot referens {kr(d.reference.median_ore)})"
    lines = [head + "."]
    if d.threshold_alert:
        if d.under_target:
            lines.append(f"Under ditt mål på {kr(d.rule.target_ore)}.")
        if d.under_percent:
            lines.append(f"Minst {d.rule.percent} % under referenspriset.")
        if d.further_drop:
            lines.append(f"Ytterligare sänkt sedan förra larmet ({kr(d.rule.last_alert_ore)}).")
    if d.all_time_low:
        lines.append(f"Lägsta pris någonsin (tidigare {kr(d.previous_low_ore)}).")
    lines.append(url)
    return f"Prislarm: {title}", "\n".join(lines)
