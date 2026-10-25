"""Adding and removing watched lists, shared by the CLI and the web UI.

- An Amazon list is stored under its list id with a clean URL. Without a name it takes
  Amazon's title on its first omgång.
- Single products (ASIN watches) live in one `lokal` list, created on first use; the
  collector reads their product page every omgång.
- Removing a list keeps price history and runs; products in no other list become inactive.
"""
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from .amazon import list_id_from_url
from .db import utc_iso
from .parser import BASE_URL

LOCAL_LIST_NAME = "Enstaka produkter"
_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_IN_URL = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")


class ListError(ValueError):
    """A user error, with a message fit to show as is."""


def _now() -> str:
    return utc_iso(datetime.now(timezone.utc))


def is_amazon_se(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("amazon.se", "www.amazon.se")


def _check_percent(procent: int | None) -> None:
    if procent is not None and not 1 <= procent <= 99:
        raise ListError("procent måste vara 1–99")


def add_amazon_list(conn: sqlite3.Connection, url: str, namn: str | None = None, procent: int | None = None) -> int:
    url = url.strip()
    list_id = list_id_from_url(url)
    if not is_amazon_se(url) or not list_id:
        raise ListError("ange en delad önskelista på amazon.se (…/hz/wishlist/ls/<id>)")
    _check_percent(procent)
    if conn.execute("SELECT 1 FROM wishlist WHERE list_id = ?", (list_id,)).fetchone():
        raise ListError(f"listan {list_id} finns redan")
    return conn.execute(
        """INSERT INTO wishlist (typ, namn, url, list_id, standard_procent, standard_aktiv, skapad)
           VALUES ('amazon', ?, ?, ?, ?, ?, ?)""",
        (
            (namn or "").strip() or list_id, f"{BASE_URL}/hz/wishlist/ls/{list_id}", list_id,
            procent, int(procent is not None), _now(),
        ),
    ).lastrowid


def asin_from(text: str) -> str | None:
    """An ASIN, or a product link on amazon.se."""
    text = text.strip()
    if _ASIN.match(text.upper()):
        return text.upper()
    m = _ASIN_IN_URL.search(text)
    return m.group(1) if m and is_amazon_se(text) else None


def add_asin(conn: sqlite3.Connection, text: str) -> str:
    """Watch a single product. Returns the ASIN."""
    asin = asin_from(text)
    if asin is None:
        raise ListError("ange ett ASIN (10 tecken) eller en produktlänk på amazon.se")
    now = _now()
    conn.execute("BEGIN")
    try:
        row = conn.execute("SELECT id FROM wishlist WHERE typ = 'lokal' ORDER BY id LIMIT 1").fetchone()
        wl_id = row["id"] if row else conn.execute(
            "INSERT INTO wishlist (typ, namn, skapad) VALUES ('lokal', ?, ?)", (LOCAL_LIST_NAME, now)
        ).lastrowid
        member = conn.execute(
            "SELECT borttagen FROM wishlist_product WHERE wishlist_id = ? AND asin = ?", (wl_id, asin)
        ).fetchone()
        if member is not None and member["borttagen"] is None:
            raise ListError(f"{asin} bevakas redan")
        conn.execute(
            """INSERT INTO product (asin, url, forsta_sedd) VALUES (?, ?, ?)
               ON CONFLICT (asin) DO UPDATE SET aktiv = 1""",
            (asin, f"{BASE_URL}/dp/{asin}", now),
        )
        conn.execute(
            """INSERT INTO wishlist_product (wishlist_id, asin, tillagd) VALUES (?, ?, ?)
               ON CONFLICT (wishlist_id, asin) DO UPDATE SET borttagen = NULL, tillagd = excluded.tillagd""",
            (wl_id, asin, now),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return asin


def find_list(conn: sqlite3.Connection, key: str | int) -> sqlite3.Row | None:
    """By id or Amazon list id."""
    return conn.execute(
        "SELECT * FROM wishlist WHERE list_id = ? OR CAST(id AS TEXT) = ?", (str(key), str(key))
    ).fetchone()


def remove_list(conn: sqlite3.Connection, key: str | int) -> tuple[sqlite3.Row, int]:
    """Returns the removed list and how many products are no longer watched."""
    wl = find_list(conn, key)
    if wl is None:
        raise ListError(f"ingen lista med id eller list-id {key}")
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM wishlist WHERE id = ?", (wl["id"],))  # wishlist_product cascades
        inactive = _deactivate_orphans(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return wl, inactive


def remove_asin(conn: sqlite3.Connection, asin: str) -> None:
    """Stop a single-product watch (the product stays if it is in an Amazon list)."""
    conn.execute(
        """UPDATE wishlist_product SET borttagen = ?
           WHERE asin = ? AND borttagen IS NULL AND wishlist_id IN (SELECT id FROM wishlist WHERE typ = 'lokal')""",
        (_now(), asin),
    )
    _deactivate_orphans(conn)


def _deactivate_orphans(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """UPDATE product SET aktiv = 0 WHERE aktiv = 1 AND NOT EXISTS (
               SELECT 1 FROM wishlist_product wp WHERE wp.asin = product.asin AND wp.borttagen IS NULL)"""
    ).rowcount


def set_default_percent(conn: sqlite3.Connection, wishlist_id: int, procent: int | None) -> None:
    """None turns the list's default alert off (the last percent is kept for next time)."""
    _check_percent(procent)
    if procent is None:
        conn.execute("UPDATE wishlist SET standard_aktiv = 0 WHERE id = ?", (wishlist_id,))
    else:
        conn.execute(
            "UPDATE wishlist SET standard_procent = ?, standard_aktiv = 1 WHERE id = ?", (procent, wishlist_id)
        )
