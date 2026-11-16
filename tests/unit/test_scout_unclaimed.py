# ruff: noqa: F811  (the ``world`` fixture of test_scout.py is imported and then requested as an argument)
"""STR-16: the Known, no assets tab: structures known from docking rights, in-game mails or public datasets where
no registered character has an asset."""

import time

import pytest
from test_scout import scout_for, world  # noqa: F401
from test_scout_mail import history
from test_scout_search import JITA, P1, P2, S1, S2, S3, S5, S7, X, demo_client, ids, pub, seed_search

pytestmark = pytest.mark.anyio
CHAR = 9001


def seed_unclaimed(db) -> None:
    """S1 has assets (and a mail), S2 may be docked at, S3 is in a dataset, S4 and S6 are known by nobody else,
    S5 and S7 are named in mails; P1 and P2 are only in the public dataset."""
    seed_search(db)
    now = time.time()
    db.execute("INSERT INTO structure_sightings(structure_id, character_id, seen_at) VALUES(?,?,?)", (S1, CHAR, now))
    db.execute("INSERT INTO structure_access(structure_id, character_id, seen_at) VALUES(?,?,?)", (S2, CHAR, now))
    history(db, S3, name="Perimeter - Gamma", owner_id=X, system_id=30000144, type_id=35832)
    db.execute(
        "INSERT INTO structure_mail_finds(structure_id, owner_id, type_id, name, system_id, mail_id, mail_ts, "
        "found_at) VALUES(?,?,?,?,?,?,?,1)",
        (S1, X, 35832, "Jita - Alpha", JITA, 7, 1_700_000_000.0),
    )


async def test_only_structures_known_by_a_way_other_than_assets_are_listed(settings, services):
    seed_unclaimed(services["db"])
    got = scout_for(settings, services).unclaimed({}, [], public=pub())
    assert ids(got) == {S2, S3, S5, S7, P1, P2}  # not S1 (assets), S4 and S6 (nothing else knows them)
    by_id = {r["structure_id"]: r for r in got["rows"]}
    assert by_id[S2]["known_via"] == ["dock"] and by_id[S3]["known_via"] == ["registry"]
    assert by_id[S5]["known_via"] == ["mail"] and by_id[P1]["known_via"] == ["registry"]
    assert by_id[P1]["origin"] == "public" and by_id[S2]["origin"] == "scout"
    assert not any(r["assets"] for r in got["rows"])
    assert got["facets"]["via"] == {"dock": 1, "mail": 2, "registry": 3} and got["facets"]["total"] == 6
    assert got["public_loaded"] is True and got["matched"] == 6


async def test_a_structure_with_assets_is_never_listed_even_when_a_dataset_and_a_mail_know_it(settings, services):
    seed_unclaimed(services["db"])
    got = scout_for(settings, services).unclaimed({}, [], public=pub())  # pub() also lists S1
    assert S1 not in ids(got)
    db = services["db"]
    db.execute("DELETE FROM structure_sightings")  # the character no longer has assets there
    scout = scout_for(settings, services)
    assert S1 in ids(scout.unclaimed({}, [], public=pub()))
    assert next(r for r in scout.unclaimed({}, [], public=pub())["rows"] if r["structure_id"] == S1)["known_via"] == [
        "mail"
    ]


async def test_a_destroyed_structure_is_not_listed(settings, services):
    db = services["db"]
    seed_unclaimed(db)
    db.execute("UPDATE known_structures SET destroyed_at=? WHERE structure_id=?", (time.time(), S2))
    assert S2 not in ids(scout_for(settings, services).unclaimed({}, [], public=pub()))


async def test_the_filters_narrow_the_list_and_the_facets_stay_whole(settings, services):
    seed_unclaimed(services["db"])
    scout = scout_for(settings, services)
    kw = {"public": pub()}
    assert ids(scout.unclaimed({}, [], via="mail", **kw)) == {S5, S7}
    assert ids(scout.unclaimed({}, [], via="dock", **kw)) == {S2}
    assert ids(scout.unclaimed({}, [], via="registry", **kw)) == {S3, P1, P2}
    assert ids(scout.unclaimed({}, [], via="nonsense", **kw)) == {S2, S3, S5, S7, P1, P2}  # anything else: any
    assert ids(scout.unclaimed({}, [], q="public hall", **kw)) == {P1}
    narrowed = scout.unclaimed({}, [], space="high", via="mail", **kw)
    assert ids(narrowed) == {S5} and narrowed["facets"]["total"] == 6  # facets are taken before the filters
    assert ids(scout.unclaimed({}, [], q="nothing like this", **kw)) == set()


async def test_without_the_public_dataset_only_the_scout_rows_are_listed(settings, services):
    seed_unclaimed(services["db"])
    got = scout_for(settings, services).unclaimed({}, [])
    assert ids(got) == {S2, S3, S5, S7} and got["public_loaded"] is False


async def test_sort_limit_and_offset(settings, services):
    seed_unclaimed(services["db"])
    scout = scout_for(settings, services)
    by_name = scout.unclaimed({}, [], public=pub(), sort="name", limit=3)
    assert [r["name"].lower() for r in by_name["rows"]] == sorted(r["name"].lower() for r in by_name["rows"])
    assert len(by_name["rows"]) == 3 and by_name["matched"] == 6
    rest = scout.unclaimed({}, [], public=pub(), sort="name", limit=3, offset=3)
    assert not ids(by_name) & ids(rest) and len(ids(by_name) | ids(rest)) == 6


def test_the_endpoint_in_demo_lists_dataset_structures_and_never_assets(tmp_path):
    with demo_client(tmp_path) as c:
        body = c.get("/api/structures/unclaimed").json()
        assert body["matched"] >= 1 and body["public_loaded"] is True
        assert all(r["known_via"] and not r["assets"] for r in body["rows"])
        assert set(body["facets"]["via"]) == {"dock", "mail", "registry"}
        assert c.get("/api/structures/unclaimed", params={"via": "registry", "limit": 1}).json()["rows"].__len__() == 1
        assert (
            c.get("/api/structures/unclaimed", params={"via": "mail"}).json()["matched"]
            == body["facets"]["via"]["mail"]
        )
        assert c.get("/structures").status_code == 200 and "Known, no assets" in c.get("/structures").text
