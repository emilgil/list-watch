from datetime import datetime, timedelta, timezone

import pytest

from listwatch import alerts
from listwatch.collector import collect
from listwatch.db import connect, migrate, utc_iso
from listwatch.notify import AppriseNotifier, apprise_urls
from listwatch.parser import ProductPage, WishlistItem
from listwatch.source import ListRead, ProductRead

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
ASIN = "B09XS7JWHH"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeSource:
    def __init__(self):
        self.price = 399000
        self.prime: bool | None = True
        self.product_error: ProductRead | None = None
        self.product_calls = 0

    def read_list(self, url):
        item = WishlistItem(asin=ASIN, title="Sony WH-1000XM5", image_url=None,
                            price_ore=self.price, prime=None, status="pris" if self.price else "ej_i_lager")
        return ListRead(items=[item], pages=1, http_status=200)

    def read_product(self, asin):
        self.product_calls += 1
        if self.product_error:
            return self.product_error
        return ProductRead(200, page=ProductPage(self.price, "pris", self.prime, self.prime))


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.error: str | None = None

    def send(self, title, body):
        if self.error:
            return self.error
        self.sent.append((title, body))
        return None


@pytest.fixture
def env(tmp_path):
    migrate(tmp_path / "lw.db")
    conn = connect(tmp_path / "lw.db")
    conn.execute("UPDATE settings SET varde = '0' WHERE nyckel = 'max_product_pages_per_round'")
    conn.execute(
        "INSERT INTO wishlist (typ, namn, url, list_id, skapad) VALUES ('amazon', 'L1', 'L1', 'L1', ?)", (utc_iso(T0),)
    )
    e = Env(conn)
    yield e
    conn.close()


class Env:
    def __init__(self, conn):
        self.conn = conn
        self.src = FakeSource()
        self.notifier = FakeNotifier()
        self.clock = Clock()

    def round(self, price=None, hours=6):
        if price is not None:
            self.src.price = price
        [run_id] = collect(self.conn, self.src, now=self.clock, notifier=self.notifier)
        self.clock.advance(hours=hours)
        return run_id

    def history(self, days, price=399000, prime="prime"):
        """Daily Prime measurements for the last `days` days, plus a fresh Prime cache."""
        self.round()  # creates the product
        self.conn.execute("DELETE FROM price_point")
        for d in range(days, 0, -1):
            self.conn.execute(
                "INSERT INTO price_point (asin, tidpunkt, status, pris_ore, prime, kalla) VALUES (?, ?, 'pris', ?, ?, 'lista')",
                (ASIN, utc_iso(self.clock() - timedelta(days=d)), price, prime),
            )
        self.fresh_prime()

    def fresh_prime(self, status="prime"):
        self.conn.execute(
            "UPDATE product SET prime_status = ?, prime_kontrollerad = ?", (status, utc_iso(self.clock()))
        )

    def rule(self, **cols):
        cols = {"asin": ASIN, "arver_standard": 0, **cols}
        names = ", ".join(cols)
        self.conn.execute(
            f"INSERT INTO alert_rule ({names}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values())
        )

    def last_alert(self):
        row = self.conn.execute("SELECT senaste_larm_pris FROM alert_rule WHERE asin = ?", (ASIN,)).fetchone()
        return row[0] if row else None

    def run(self, run_id):
        return dict(self.conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone())

    def events(self, run_id, typ):
        return [tuple(r) for r in self.conn.execute(
            "SELECT asin, efter, text FROM event WHERE run_id = ? AND typ = ?", (run_id, typ))]


def test_fixed_target_crossing_suppression_and_reset(env):
    env.history(10)
    env.rule(mal_pris_ore=320000)
    sent = env.notifier.sent
    steps = [
        (330000, 0, None),      # above target
        (299000, 1, 299000),    # crossing: alert
        (295000, 1, 299000),    # still under, < 5 % further: suppressed
        (284050, 2, 284050),    # 5 % below last alert: alert again
        (330000, 2, None),      # above the level: reset
        (310000, 3, 310000),    # new crossing: alert
    ]
    for price, n_sent, last in steps:
        env.fresh_prime()
        env.round(price)
        assert (len(sent), env.last_alert()) == (n_sent, last), price


def test_alert_logged_and_counted(env):
    env.history(10)
    env.rule(mal_pris_ore=320000)
    run_id = env.round(299000)
    assert env.run(run_id)["larm_skickade"] == 1
    [(asin, efter, text)] = env.events(run_id, "larm_skickat")
    assert (asin, efter) == (ASIN, "299000")
    assert "Under ditt mål på 3 200 kr." in text


def test_percent_needs_seven_days_of_prime_reference(env):
    env.history(6)
    env.rule(mal_procent=20)
    env.round(300000)  # −25 % but only 6 days of data
    assert env.notifier.sent == []
    env.clock.advance(days=1)
    env.fresh_prime()
    env.round(300000)
    assert len(env.notifier.sent) == 1
    title, body = env.notifier.sent[0]
    assert title == "Prislarm: Sony WH-1000XM5"
    assert body.startswith("Sony WH-1000XM5: 3 000 kr (−25 % mot referens 3 990 kr).")
    assert "Minst 20 % under referenspriset." in body
    assert body.endswith("https://www.amazon.se/dp/B09XS7JWHH")


def test_reference_uses_only_prime_measurements(env):
    env.history(10, price=200000, prime="inte_prime")
    env.rule(mal_procent=20)
    env.round(150000)  # far below the non-Prime median, but there is no Prime reference
    assert env.notifier.sent == []


def test_inherits_lowest_percent_among_active_lists(env):
    env.history(10)
    conn = env.conn
    conn.execute("UPDATE wishlist SET standard_procent = 30, standard_aktiv = 1")
    wl2 = conn.execute(
        "INSERT INTO wishlist (typ, namn, list_id, skapad, standard_procent, standard_aktiv) VALUES ('lokal', 'L2', NULL, ?, 10, 1)",
        (utc_iso(T0),),
    ).lastrowid
    conn.execute("INSERT INTO wishlist_product (wishlist_id, asin, tillagd) VALUES (?, ?, ?)", (wl2, ASIN, utc_iso(T0)))
    rule = alerts.rule_for(conn, ASIN)
    assert (rule.active, rule.percent, rule.target_ore) == (True, 10, None)
    # an inactive default doesn't count
    conn.execute("UPDATE wishlist SET standard_aktiv = 0 WHERE id = ?", (wl2,))
    assert alerts.rule_for(conn, ASIN).percent == 30


def test_own_rule_overrides_and_can_be_switched_off(env):
    env.history(10)
    env.conn.execute("UPDATE wishlist SET standard_procent = 10, standard_aktiv = 1")
    env.rule(mal_procent=40)
    assert alerts.rule_for(env.conn, ASIN).percent == 40
    env.round(320000)  # −20 %: enough for the list default, not for the own rule
    assert env.notifier.sent == []
    env.conn.execute("UPDATE alert_rule SET mal_pris_ore = 330000, larm_aktivt = 0")
    env.fresh_prime()
    env.round(320000)
    assert env.notifier.sent == []


def test_unknown_prime_is_checked_before_alerting(env):
    env.history(10)
    env.fresh_prime("okänd")
    env.rule(mal_pris_ore=320000)
    run_id = env.round(299000)
    assert env.src.product_calls == 1
    assert len(env.notifier.sent) == 1
    assert env.events(run_id, "produktsida_hämtad") == [(ASIN, None, "larmkontroll")]
    point = env.conn.execute("SELECT prime FROM price_point ORDER BY id DESC").fetchone()[0]
    assert point == "prime"


def test_stale_prime_is_rechecked_and_non_prime_does_not_alert(env):
    env.history(10)
    env.rule(mal_pris_ore=320000)
    env.clock.advance(hours=25)  # cache older than prime_fresh_for_alert (24 h)
    env.src.prime = False
    run_id = env.round(299000)
    assert env.src.product_calls == 1
    assert env.notifier.sent == [] and env.last_alert() is None
    assert env.events(run_id, "prime_ändrad") == [(ASIN, "inte_prime", None)]
    # fresh inte_prime: no new check, still no alert
    env.round(299000)
    assert env.src.product_calls == 1 and env.notifier.sent == []


def test_fresh_prime_needs_no_product_page(env):
    env.history(10)
    env.rule(mal_pris_ore=320000)
    env.round(299000)
    assert env.src.product_calls == 0 and len(env.notifier.sent) == 1


def test_failed_prime_check_sends_nothing_and_keeps_suppression(env):
    env.history(10)
    env.fresh_prime("okänd")
    env.rule(mal_pris_ore=320000)
    env.src.product_error = ProductRead(200, captcha=True)
    run_id = env.round(299000)
    assert env.notifier.sent == [] and env.last_alert() is None
    assert env.run(run_id)["captcha"] == "produktsida"
    env.src.product_error = None
    env.round(299000)
    assert len(env.notifier.sent) == 1


def test_failed_send_is_logged_and_retried(env):
    env.history(10)
    env.rule(mal_pris_ore=320000)
    env.notifier.error = "Connection error"
    run_id = env.round(299000)
    r = env.run(run_id)
    assert (r["larm_skickade"], r["larm_misslyckade"], r["resultat"]) == (0, 1, "delvis")
    assert env.events(run_id, "larm_misslyckades") == [(ASIN, "299000", "Connection error")]
    assert env.last_alert() is None
    env.notifier.error = None
    env.fresh_prime()
    env.round(299000)
    assert len(env.notifier.sent) == 1 and env.last_alert() == 299000


def test_out_of_stock_does_not_reset_suppression(env):
    env.history(10)
    env.rule(mal_pris_ore=320000)
    env.round(299000)
    env.round(0)  # ej_i_lager
    env.fresh_prime()
    env.round(299000)
    assert len(env.notifier.sent) == 1


def test_suspicious_price_alerts_only_when_confirmed(env):
    env.history(10)
    env.rule(mal_pris_ore=320000)
    env.round(19900)  # < 10 % of the median: suspicious
    assert env.notifier.sent == []
    env.fresh_prime()
    env.round(19900)  # confirmed by the next measurement
    assert len(env.notifier.sent) == 1


def test_all_time_low_after_30_days(env):
    env.history(29)
    env.rule(lagsta_nagonsin_notis=1)
    env.round(398000)
    assert env.notifier.sent == []  # only 29 days of history
    env.clock.advance(days=1)
    env.fresh_prime()
    env.round(397000)
    [(_, body)] = env.notifier.sent
    assert "Lägsta pris någonsin (tidigare 3 980 kr)." in body
    assert env.last_alert() is None  # all-time low doesn't set the threshold suppression


@pytest.mark.parametrize("ore, text", [(299000, "2 990 kr"), (11566, "115,66 kr"), (1299905, "12 999,05 kr")])
def test_kr_format(ore, text):
    assert alerts.kr(ore) == text


def test_apprise_notifier_reports_missing_and_invalid_urls(env):
    assert AppriseNotifier([]).send("t", "b") == "inga Apprise-URL:er inställda"
    assert "ogiltig URL" in AppriseNotifier(["nonsense://x"]).send("t", "b")
    env.conn.execute("INSERT INTO settings (nyckel, varde) VALUES ('apprise_urls', ' json://a \n\n ntfy://b ')")
    assert apprise_urls(env.conn) == ["json://a", "ntfy://b"]
