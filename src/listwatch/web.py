"""Web UI and /health, served by `listwatch serve` in the same process as the scheduler."""
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .db import connect, latest_version, schema_version, setting
from .scheduler import Scheduler

TZ = ZoneInfo("Europe/Stockholm")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def local_time(value: str | datetime | None) -> str:
    if not value:
        return "–"
    t = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


templates.env.filters["lokal_tid"] = local_time


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


def create_app(db_path: Path | str, scheduler: Scheduler | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            scheduler.shutdown()

    app = FastAPI(title="listwatch", lifespan=lifespan, docs_url=None, redoc_url=None)

    def next_run(conn: sqlite3.Connection) -> str | None:
        at = scheduler.next_run() if scheduler else None
        return local_time(at) if at else local_time(setting(conn, "nasta_korning") or None)

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            conn = connect(db_path)
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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        conn = connect(db_path)
        try:
            lists = conn.execute(
                """SELECT w.*, (SELECT count(*) FROM wishlist_product wp
                                WHERE wp.wishlist_id = w.id AND wp.borttagen IS NULL) AS produkter
                   FROM wishlist w ORDER BY w.id"""
            ).fetchall()
            context = {"lists": lists, "next_run": next_run(conn), "last": _last_omgang(conn)}
        finally:
            conn.close()
        return templates.TemplateResponse(request, "index.html", context)

    return app
