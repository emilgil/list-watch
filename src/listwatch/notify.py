"""Notifications through Apprise (as a library, no Apprise server)."""
import logging
import sqlite3
from typing import Protocol


class Notifier(Protocol):
    def send(self, title: str, body: str) -> str | None:
        """Send a notification. None on success, otherwise an error message."""
        ...


class AppriseNotifier:
    def __init__(self, urls: list[str]):
        self.urls = urls

    def send(self, title: str, body: str) -> str | None:
        import apprise

        if not self.urls:
            return "inga Apprise-URL:er inställda"
        a = apprise.Apprise()
        bad = [u for u in self.urls if not a.add(u)]
        with apprise.LogCapture(level=logging.WARNING) as log:
            ok = bool(len(a)) and bool(a.notify(title=title, body=body))
        if ok and not bad:
            return None
        errors = [f"ogiltig URL: {u.split('://')[0]}://…" for u in bad]
        errors += [line.split(" - ", 2)[-1] for line in log.getvalue().splitlines() if line.strip()]
        return "; ".join(errors) or "Apprise kunde inte skicka"


def apprise_urls(conn: sqlite3.Connection) -> list[str]:
    """Apprise URLs from settings, one per line."""
    row = conn.execute("SELECT varde FROM settings WHERE nyckel = 'apprise_urls'").fetchone()
    return [u.strip() for u in (row[0] if row else "").splitlines() if u.strip()]


def notifier_from_settings(conn: sqlite3.Connection) -> AppriseNotifier:
    return AppriseNotifier(apprise_urls(conn))
