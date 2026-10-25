"""When omgångar run: interval, campaign interval, random delay, backoff and ops notification.

Rules (spec K3, "Hövlighet mot Amazon", "Drift"):
- The interval is `intervall_timmar` (lowest 1 h). While `kampanj_slut` is in the future and
  `kampanj_intervall_timmar` > 0, the campaign interval applies instead.
- Every scheduled start gets a random 0..`fordrojning_minuter` minutes on top.
- A blocked omgång (CAPTCHA or HTTP 503) backs off exponentially: interval × 2^n, n = blocked
  omgångar in a row (`blockerade_i_rad`), capped at MAX_BACKOFF but never below the interval.
  A non-blocked omgång resets the count. Manual omgångar count too.
- When the count reaches BLOCKED_ALERT an ops notification is sent once (a `drift_notis` event).
- The next start is stored in `nasta_korning`, so a restart keeps the schedule. A start missed
  while the service was down runs STARTUP_DELAY after startup.
- Old runs and events are purged once a day.
- Only one omgång runs at a time; a scheduled start that finds one running is skipped.
"""
import logging
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .amazon import AmazonSource
from .collector import CollectResult, Progress, collect
from .db import connect, purge_logs, set_setting, setting, setting_int, utc_iso
from .fetcher import Fetch, FetchResult, make_fetcher, polite
from .notify import Notifier, notifier_from_settings
from .source import Source

MIN_INTERVAL = timedelta(hours=1)
MAX_BACKOFF = timedelta(hours=24)
BLOCKED_ALERT = 3
STARTUP_DELAY = timedelta(minutes=2)
PURGE_AT = (3, 30)  # local time, Europe/Stockholm
TZ = "Europe/Stockholm"

Clock = Callable[[], datetime]
log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def amazon_source(on_fetch: Progress | None = None) -> Source:
    """A fresh polite source per omgång, so its first request is not delayed.

    `on_fetch` gets each URL right before it is requested (after the politeness delay).
    """
    fetch: Fetch = make_fetcher()
    if on_fetch:
        inner = fetch

        def fetch(url: str) -> FetchResult:
            on_fetch(url)
            return inner(url)

    return AmazonSource(polite(fetch))


def interval(conn: sqlite3.Connection, now: datetime) -> timedelta:
    hours = setting_int(conn, "intervall_timmar")
    campaign_end = _parse_utc(setting(conn, "kampanj_slut"))
    campaign_hours = setting_int(conn, "kampanj_intervall_timmar")
    if campaign_hours > 0 and campaign_end and campaign_end > now:
        hours = campaign_hours
    return max(timedelta(hours=hours), MIN_INTERVAL)


def next_start(conn: sqlite3.Connection, now: datetime, rng: random.Random = random) -> datetime:
    wait = interval(conn, now)
    blocked = setting_int(conn, "blockerade_i_rad")
    if blocked:
        wait = max(wait, min(wait * 2**blocked, MAX_BACKOFF))
    delay = timedelta(minutes=rng.uniform(0, setting_int(conn, "fordrojning_minuter")))
    return now + wait + delay


def after_omgang(conn: sqlite3.Connection, res: CollectResult, now: datetime, notifier: Notifier) -> None:
    """Update the blocked count and send the ops notification when it reaches BLOCKED_ALERT."""
    blocked = setting_int(conn, "blockerade_i_rad") + 1 if res.blocked else 0
    set_setting(conn, "blockerade_i_rad", blocked)
    if blocked != BLOCKED_ALERT:
        return
    body = f"Amazon har blockerat {blocked} omgångar i rad (CAPTCHA eller HTTP 503). Insamlingen backar av."
    error = notifier.send("listwatch: blockerad av Amazon", body)
    conn.execute(
        "INSERT INTO event (run_id, tid, typ, text) VALUES (?, ?, 'drift_notis', ?)",
        (res.run_ids[-1] if res.run_ids else None, utc_iso(now), body if error is None else f"{body} Misslyckades: {error}"),
    )


def run_omgang(
    conn: sqlite3.Connection,
    source: Source,
    typ: str = "schemalagd",
    now: Clock = _utcnow,
    notifier: Notifier | None = None,
    progress: Progress | None = None,
) -> CollectResult:
    notifier = notifier or notifier_from_settings(conn)
    res = collect(conn, source, typ=typ, now=now, notifier=notifier, progress=progress)
    after_omgang(conn, res, now(), notifier)
    return res


def _log_result(conn: sqlite3.Connection, res: CollectResult) -> None:
    for i in res:
        r = conn.execute("SELECT * FROM run WHERE id = ?", (i,)).fetchone()
        log.info(
            "omgång %d, %s: %s, %d lästa, %d prisändrade, %d nya, %d borttagna, %d produktsidor, "
            "%d larm, %d larm misslyckade, %d fel%s",
            res.omgang, r["list_namn"], r["resultat"], r["lasta"], r["prisandrade"], r["nya"], r["borttagna"],
            r["produktsidor"], r["larm_skickade"], r["larm_misslyckade"], r["fel"],
            f", CAPTCHA på {r['captcha']}" if r["captcha"] else "",
        )
    if res.blocked:
        log.warning("omgång %d blockerad av Amazon (CAPTCHA eller HTTP 503)", res.omgang)


class Scheduler:
    """APScheduler wrapper. Each job opens its own connection (jobs run in worker threads)."""

    def __init__(
        self,
        db_path: Path | str,
        source_factory: Callable[[], Source] = amazon_source,
        notifier: Notifier | None = None,
        now: Clock = _utcnow,
    ):
        from apscheduler.schedulers.background import BackgroundScheduler

        self.db_path = db_path
        self.source_factory = source_factory
        self.notifier = notifier
        self.now = now
        self._lock = threading.Lock()
        self._aps = BackgroundScheduler(timezone=TZ)

    def start(self, paused: bool = False) -> None:
        conn = connect(self.db_path)
        try:
            planned = _parse_utc(setting(conn, "nasta_korning"))
        finally:
            conn.close()
        earliest = self.now() + STARTUP_DELAY
        self._plan(max(planned, earliest) if planned else earliest)
        self._aps.add_job(
            self._purge, "cron", id="rensning", hour=PURGE_AT[0], minute=PURGE_AT[1], replace_existing=True
        )
        self._aps.start(paused=paused)

    def shutdown(self) -> None:
        self._aps.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return self._aps.running

    def next_run(self) -> datetime | None:
        job = self._aps.get_job("omgang")
        return job.next_run_time if job else None

    def reschedule(self) -> datetime:
        """Plan the next start from now with current settings (after a settings change)."""
        conn = connect(self.db_path)
        try:
            at = next_start(conn, self.now())
            set_setting(conn, "nasta_korning", utc_iso(at))
        finally:
            conn.close()
        self._plan(at)
        return at

    def run_now(self, typ: str = "manuell") -> CollectResult | None:
        """Run an omgång in the calling thread. None if one is already running."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            conn = connect(self.db_path)
            try:
                res = run_omgang(conn, self.source_factory(), typ=typ, now=self.now, notifier=self.notifier)
                _log_result(conn, res)
                return res
            finally:
                conn.close()
        finally:
            self._lock.release()

    def _plan(self, at: datetime) -> None:
        # No misfire grace limit: a start missed while the process was suspended still runs
        # (late), otherwise the chain of one-shot jobs would end there.
        self._aps.add_job(
            self._scheduled, "date", id="omgang", run_date=at, replace_existing=True, misfire_grace_time=None
        )

    def _scheduled(self) -> None:
        try:
            if self.run_now(typ="schemalagd") is None:
                log.info("omgång pågår redan, schemalagd start hoppas över")
        except Exception:
            log.exception("omgång misslyckades")
        finally:
            self.reschedule()

    def _purge(self) -> None:
        conn = connect(self.db_path)
        try:
            res = purge_logs(conn, self.now())
            log.info("loggrensning: %d rundor, %d händelser", res.runs, res.events)
        except Exception:
            log.exception("loggrensning misslyckades")
        finally:
            conn.close()
