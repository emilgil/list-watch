import pytest

from listwatch import cli, scheduler
from listwatch.amazon import list_id_from_url
from listwatch.db import connect, migrate
from test_collector import FakeSource, item, ok
from test_scheduler import FakeNotifier

URL = "https://www.amazon.se/hz/wishlist/ls/3EWS08VXURE1W?ref_=wl_share"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "lw.db"
    migrate(path)
    return path


def wishlists(db_path):
    c = connect(db_path)
    try:
        return [dict(r) for r in c.execute("SELECT * FROM wishlist")]
    finally:
        c.close()


def test_list_id_from_url():
    assert list_id_from_url(URL) == "3EWS08VXURE1W"
    assert list_id_from_url("https://www.amazon.se/dp/B0C93G2M83") is None


def test_add_list_stores_a_clean_url(db_path, capsys):
    assert cli.main(["add-list", URL, "--db", str(db_path)]) == 0
    [wl] = wishlists(db_path)
    assert (wl["typ"], wl["namn"], wl["list_id"]) == ("amazon", "3EWS08VXURE1W", "3EWS08VXURE1W")
    assert wl["url"] == "https://www.amazon.se/hz/wishlist/ls/3EWS08VXURE1W"
    assert (wl["standard_procent"], wl["standard_aktiv"]) == (None, 0)
    assert "Inget standardlarm" in capsys.readouterr().out


def test_add_list_with_name_and_percent(db_path):
    assert cli.main(["add-list", URL, "--namn", "Hemmet", "--procent", "20", "--db", str(db_path)]) == 0
    [wl] = wishlists(db_path)
    assert (wl["namn"], wl["standard_procent"], wl["standard_aktiv"]) == ("Hemmet", 20, 1)


def test_add_list_twice_is_refused(db_path, capsys):
    cli.main(["add-list", URL, "--db", str(db_path)])
    assert cli.main(["add-list", URL, "--db", str(db_path)]) == 1
    assert "finns redan" in capsys.readouterr().err
    assert len(wishlists(db_path)) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["https://www.amazon.com/hz/wishlist/ls/3EWS08VXURE1W"],
        ["https://www.amazon.se/dp/B0C93G2M83"],
        [URL, "--procent", "0"],
    ],
)
def test_add_list_rejects_bad_input(db_path, args):
    assert cli.main(["add-list", *args, "--db", str(db_path)]) == 2
    assert wishlists(db_path) == []


def test_add_list_requires_migrated_db(tmp_path, capsys):
    assert cli.main(["add-list", URL, "--db", str(tmp_path / "new.db")]) == 2
    assert "listwatch migrate" in capsys.readouterr().err


def test_first_omgang_takes_amazons_list_name(db_path, monkeypatch):
    cli.main(["add-list", URL, "--db", str(db_path)])
    cli.main(["add-list", URL.replace("3EWS08VXURE1W", "OTHER123"), "--namn", "Egen", "--db", str(db_path)])
    src = FakeSource()
    read = ok(item("A000000001", 100))
    read.title = "Hemmet"
    src.lists = {wl["url"]: read for wl in wishlists(db_path)}
    monkeypatch.setattr(scheduler, "amazon_source", lambda on_fetch=None: src)
    monkeypatch.setattr(scheduler, "notifier_from_settings", lambda conn: FakeNotifier())
    cli.main(["collect", "--db", str(db_path)])
    assert [wl["namn"] for wl in wishlists(db_path)] == ["Hemmet", "Egen"]
