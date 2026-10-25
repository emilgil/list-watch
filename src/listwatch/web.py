"""Web UI and /health, served by `listwatch serve` in the same process as the scheduler.

Pages (spec "Webbgränssnitt" and docs/spec-logg.md): Översikt, Listor, Produkt, Logg,
Inställningar. No login (LAN/Tailscale only). Forms post and redirect back with a short
message in `?ok=` / `?fel=`; small toggles and "synka nu" are HTMX fragments.
"""
import json
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import lists, webdata
from .alerts import kr
from .db import connect, latest_version, schema_version, set_setting, setting, utc_iso
from .notify import Notifier, notifier_from_settings
from .prices import parse_price_ore
from .scheduler import Scheduler, interval

TZ = ZoneInfo("Europe/Stockholm")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
log = logging.getLogger(__name__)

EVENT_TYPES = (
    "pris_ändrat", "ny_produkt", "borttagen_produkt", "ej_i_lager", "åter_i_lager", "prime_ändrad",
    "produktsida_hämtad", "larm_skickat", "larm_misslyckades", "saknar_pris", "http_fel",
    "parsningsfel", "captcha", "drift_notis", "test_notis",
)
SCHEDULE_SETTINGS = ("intervall_timmar", "kampanj_intervall_timmar", "kampanj_slut", "fordrojning_minuter")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_time(value: str | datetime | None) -> str:
    if not value:
        return "–"
    t = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def _kr(ore: int | None) -> str:
    return "–" if ore is None else kr(ore)


def _event_value(value: str | None, typ: str) -> str:
    """Event before/after values are raw (öre, status names); show prices in kr."""
    if value is None:
        return ""
    if typ in ("pris_ändrat", "åter_i_lager", "larm_skickat", "larm_misslyckades") and value.isdigit():
        return kr(int(value))
    return value.replace("_", " ")


templates.env.filters["lokal_tid"] = local_time
templates.env.filters["kr"] = _kr
templates.env.filters["handelse"] = _event_value
templates.env.filters["todt"] = lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))


class HealthLogFilter(logging.Filter):
    """Keep /health polling (install.sh, monitoring) out of the uvicorn access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3 and str(args[2]).startswith("/health"))


def _last_omgang(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """SELECT omgang, max(slut) AS slut, group_concat(resultat) AS resultat FROM run
           WHERE typ != 'probe' AND omgang = (SELECT max(omgang) FROM run WHERE typ != 'probe')"""
    ).fetchone()
    if row["omgang"] is None:
        return None
    results = (row["resultat"] or "").split(",")
    worst = next((r for r in ("fel", "delvis", "ok") if r in results), None)  # None while running
    return {"omgang": row["omgang"], "slut": row["slut"], "resultat": worst}


def _redirect(path: str, **message: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in message.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _int(value: str, name: str, low: int, high: int | None = None) -> int:
    try:
        n = int(value.strip())
    except ValueError:
        raise ValueError(f"{name} måste vara ett heltal") from None
    if n < low or (high is not None and n > high):
        raise ValueError(f"{name} måste vara {low}–{high}" if high is not None else f"{name} måste vara minst {low}")
    return n


def _local_input(value: str) -> str:
    """A datetime-local field (Stockholm time) as UTC ISO; '' stays ''."""
    if not value.strip():
        return ""
    try:
        t = datetime.fromisoformat(value.strip()).replace(tzinfo=TZ)
    except ValueError:
        raise ValueError("kampanjens slut är inte ett giltigt datum") from None
    return utc_iso(t)


def _utc_to_input(value: str) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


def create_app(
    db_path: Path | str,
    scheduler: Scheduler | None = None,
    notifier: Notifier | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            scheduler.shutdown()

    app = FastAPI(title="listwatch", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.sync_thread = None

    def db() -> sqlite3.Connection:
        return connect(db_path)

    def next_run(conn: sqlite3.Connection) -> str:
        at = scheduler.next_run() if scheduler else None
        return local_time(at) if at else local_time(setting(conn, "nasta_korning") or None)

    def page(request: Request, name: str, conn: sqlite3.Connection, **context) -> HTMLResponse:
        context.setdefault("ok", request.query_params.get("ok"))
        context.setdefault("fel", request.query_params.get("fel"))
        context["status"] = {
            "next_run": next_run(conn),
            "last": _last_omgang(conn),
            "blocked": int(setting(conn, "blockerade_i_rad")),
            "syncing": _syncing(),
        }
        return templates.TemplateResponse(request, name, context)

    def _syncing() -> bool:
        t = app.state.sync_thread
        return t is not None and t.is_alive()

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            conn = db()
            try:
                version = schema_version(conn)
                body = {
                    "status": "ok" if version == latest_version() else "fel",
                    "schema": version,
                    "schemalaggare": scheduler is not None and scheduler.running,
                    "nasta_korning": next_run(conn),
                    "senaste_omgang": _last_omgang(conn),
                    "blockerade_i_rad": int(setting(conn, "blockerade_i_rad")),
                }
            finally:
                conn.close()
        except (sqlite3.Error, KeyError) as e:
            return JSONResponse({"status": "fel", "fel": str(e)}, status_code=503)
        if scheduler is not None and not scheduler.running:
            body["status"] = "fel"
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    # Översikt -------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, lista: str = "", sort: str = "rabatt"):
        conn = db()
        try:
            list_id = int(lista) if lista.isdigit() else None
            return page(
                request, "overview.html", conn,
                rows=webdata.overview(conn, now(), lista=list_id, sort=sort),
                lists=webdata.lists_overview(conn), lista=list_id, sort=sort,
            )
        finally:
            conn.close()

    @app.post("/produkt/{asin}/larm", response_class=HTMLResponse)
    def toggle_alert(request: Request, asin: str):
        conn = db()
        try:
            if conn.execute("SELECT 1 FROM product WHERE asin = ?", (asin,)).fetchone() is None:
                raise HTTPException(404)
            conn.execute(
                """INSERT INTO alert_rule (asin, larm_aktivt) VALUES (?, 0)
                   ON CONFLICT (asin) DO UPDATE SET larm_aktivt = 1 - larm_aktivt""",
                (asin,),
            )
            row = webdata.overview_row(conn, asin, now())
        finally:
            conn.close()
        return templates.TemplateResponse(request, "_alert_actions.html", {"r": row})

    # Produkt --------------------------------------------------------------------------

    @app.get("/produkt/{asin}", response_class=HTMLResponse)
    def product(request: Request, asin: str, period: str = "90d"):
        conn = db()
        try:
            view = webdata.product_view(conn, asin, now(), period if period in webdata.PERIODS else "90d")
            if view is None:
                raise HTTPException(404, "okänd produkt")
            return page(
                request, "product.html", conn, v=view, period=period,
                chart=json.dumps(webdata.chart_data(view)), periods=webdata.PERIODS,
            )
        finally:
            conn.close()

    @app.post("/produkt/{asin}/regel")
    def save_rule(
        asin: str,
        mal_pris: str = Form(""),
        mal_procent: str = Form(""),
        arver_standard: str | None = Form(None),
        lagsta_nagonsin: str | None = Form(None),
        larm_aktivt: str | None = Form(None),
    ):
        path = f"/produkt/{asin}"
        try:
            target = None
            if mal_pris.strip():
                target = parse_price_ore(mal_pris)
                if not target:
                    raise ValueError("målpriset är inte ett giltigt belopp")
            percent = _int(mal_procent, "procent", 1, 99) if mal_procent.strip() else None
        except ValueError as e:
            return _redirect(path, fel=str(e))
        conn = db()
        try:
            if conn.execute("SELECT 1 FROM product WHERE asin = ?", (asin,)).fetchone() is None:
                raise HTTPException(404)
            conn.execute(
                """INSERT INTO alert_rule (asin, mal_pris_ore, mal_procent, arver_standard,
                                           lagsta_nagonsin_notis, larm_aktivt)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (asin) DO UPDATE SET
                       mal_pris_ore = excluded.mal_pris_ore, mal_procent = excluded.mal_procent,
                       arver_standard = excluded.arver_standard,
                       lagsta_nagonsin_notis = excluded.lagsta_nagonsin_notis,
                       larm_aktivt = excluded.larm_aktivt""",
                (asin, target, percent, int(arver_standard is not None), int(lagsta_nagonsin is not None),
                 int(larm_aktivt is not None)),
            )
        finally:
            conn.close()
        return _redirect(path, ok="Larmregeln är sparad")

    @app.post("/produkt/{asin}/sluta-bevaka")
    def stop_watching(asin: str):
        conn = db()
        try:
            lists.remove_asin(conn, asin)
        finally:
            conn.close()
        return _redirect("/listor", ok=f"{asin} bevakas inte längre som enstaka produkt")

    # Listor ---------------------------------------------------------------------------

    @app.get("/listor", response_class=HTMLResponse)
    def lists_page(request: Request):
        conn = db()
        try:
            local = conn.execute(
                """SELECT p.asin, p.titel FROM product p
                   JOIN wishlist_product wp ON wp.asin = p.asin AND wp.borttagen IS NULL
                   JOIN wishlist w ON w.id = wp.wishlist_id AND w.typ = 'lokal' ORDER BY p.titel, p.asin"""
            ).fetchall()
            return page(request, "lists.html", conn, lists=webdata.lists_overview(conn), local=local)
        finally:
            conn.close()

    @app.post("/listor")
    def add(lank: str = Form(...), namn: str = Form(""), procent: str = Form("")):
        conn = db()
        try:
            if lists.asin_from(lank):
                asin = lists.add_asin(conn, lank)
                return _redirect("/listor", ok=f"{asin} bevakas; produktsidan läses vid nästa omgång")
            pct = _int(procent, "procent", 1, 99) if procent.strip() else None
            wl = lists.find_list(conn, lists.add_amazon_list(conn, lank, namn, pct))
            return _redirect("/listor", ok=f"Listan {wl['namn']} är tillagd; den läses vid nästa omgång")
        except ValueError as e:  # ListError too
            return _redirect("/listor", fel=str(e))
        finally:
            conn.close()

    @app.post("/listor/{wishlist_id}/procent")
    def set_percent(wishlist_id: int, procent: str = Form("")):
        conn = db()
        try:
            pct = _int(procent, "procent", 1, 99) if procent.strip() else None
            lists.set_default_percent(conn, wishlist_id, pct)
        except ValueError as e:
            return _redirect("/listor", fel=str(e))
        finally:
            conn.close()
        return _redirect("/listor", ok="Standardlarmet är sparat" if pct else "Standardlarmet är avstängt")

    @app.post("/listor/{wishlist_id}/ta-bort")
    def remove(wishlist_id: int):
        conn = db()
        try:
            wl, inactive = lists.remove_list(conn, wishlist_id)
        except lists.ListError as e:
            return _redirect("/listor", fel=str(e))
        finally:
            conn.close()
        return _redirect(
            "/listor", ok=f"{wl['namn']} är borttagen; {inactive} produkter bevakas inte längre, historiken finns kvar"
        )

    @app.post("/synka", response_class=HTMLResponse)
    def sync_now(request: Request):
        if scheduler is None:
            message = "Schemaläggaren kör inte"
        elif _syncing():
            message = "En omgång pågår redan"
        else:
            def run():
                try:
                    if scheduler.run_now() is None:
                        log.info("synka nu: en omgång pågår redan")
                except Exception:
                    log.exception("synka nu misslyckades")

            app.state.sync_thread = threading.Thread(target=run, name="synka-nu", daemon=True)
            app.state.sync_thread.start()
            message = "Omgången har startat; den tar några minuter"
        return HTMLResponse(f'<span class="muted">{message}</span>')

    # Logg -----------------------------------------------------------------------------

    @app.get("/logg", response_class=HTMLResponse)
    def log_page(request: Request, asin: str = "", typ: str = ""):
        conn = db()
        try:
            if asin or typ:
                return page(
                    request, "events.html", conn, run=None, asin=asin, typ=typ, types=EVENT_TYPES,
                    events=webdata.events(conn, asin=asin or None, typ=typ or None),
                    product=conn.execute("SELECT * FROM product WHERE asin = ?", (asin,)).fetchone(),
                )
            return page(request, "log.html", conn, runs=webdata.runs(conn))
        finally:
            conn.close()

    @app.get("/logg/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int, typ: str = ""):
        conn = db()
        try:
            run = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise HTTPException(404, "okänd runda")
            return page(
                request, "events.html", conn, run=run, asin="", typ=typ, types=EVENT_TYPES,
                events=webdata.events(conn, run_id=run_id, typ=typ or None), product=None,
            )
        finally:
            conn.close()

    # Inställningar --------------------------------------------------------------------

    @app.get("/installningar", response_class=HTMLResponse)
    def settings_page(request: Request):
        conn = db()
        try:
            s = {k: setting(conn, k) for k in (
                "apprise_urls", "intervall_timmar", "kampanj_intervall_timmar", "fordrojning_minuter",
                "logg_lagringstid_dagar", "notislogg_lagringstid_dagar",
            ) if conn.execute("SELECT 1 FROM settings WHERE nyckel = ?", (k,)).fetchone()}
            s.setdefault("apprise_urls", "")
            s["kampanj_slut"] = _utc_to_input(setting(conn, "kampanj_slut"))
            return page(
                request, "settings.html", conn, s=s, notices=webdata.notification_log(conn),
                current_interval=int(interval(conn, now()).total_seconds() // 3600),
                scheduler_running=scheduler is not None and scheduler.running, schema=schema_version(conn),
            )
        finally:
            conn.close()

    @app.post("/installningar")
    def save_settings(
        apprise_urls: str = Form(""),
        intervall_timmar: str = Form(...),
        kampanj_intervall_timmar: str = Form("0"),
        kampanj_slut: str = Form(""),
        fordrojning_minuter: str = Form(...),
        logg_lagringstid_dagar: str = Form(...),
        notislogg_lagringstid_dagar: str = Form(...),
    ):
        try:
            values = {
                "apprise_urls": "\n".join(u.strip() for u in apprise_urls.splitlines() if u.strip()),
                "intervall_timmar": _int(intervall_timmar, "intervallet", 1),
                "kampanj_intervall_timmar": _int(kampanj_intervall_timmar or "0", "kampanjintervallet", 0),
                "kampanj_slut": _local_input(kampanj_slut),
                "fordrojning_minuter": _int(fordrojning_minuter, "fördröjningen", 0, 180),
                "logg_lagringstid_dagar": _int(logg_lagringstid_dagar, "loggens lagringstid", 1),
                "notislogg_lagringstid_dagar": _int(notislogg_lagringstid_dagar, "notisloggens lagringstid", 1),
            }
        except ValueError as e:
            return _redirect("/installningar", fel=str(e))
        conn = db()
        try:
            schedule_changed = any(setting(conn, k) != str(values[k]) for k in SCHEDULE_SETTINGS)
            conn.execute("BEGIN")
            for key, value in values.items():
                set_setting(conn, key, value)
            conn.execute("COMMIT")
        finally:
            conn.close()
        if schedule_changed and scheduler is not None and scheduler.running:
            at = scheduler.reschedule()
            return _redirect("/installningar", ok=f"Inställningarna är sparade; nästa omgång {local_time(at)}")
        return _redirect("/installningar", ok="Inställningarna är sparade")

    @app.post("/installningar/testnotis", response_class=HTMLResponse)
    def test_notice():
        conn = db()
        try:
            n = notifier or notifier_from_settings(conn)
            error = n.send("listwatch: testnotis", "Det här är en testnotis från listwatch.")
            conn.execute(
                "INSERT INTO event (tid, typ, text) VALUES (?, 'test_notis', ?)",
                (utc_iso(now()), "skickad" if error is None else f"misslyckades: {error}"),
            )
        finally:
            conn.close()
        if error:
            return HTMLResponse(f'<span class="fel">Misslyckades: {_escape(error)}</span>')
        return HTMLResponse('<span class="ok">Testnotisen är skickad</span>')

    return app


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
