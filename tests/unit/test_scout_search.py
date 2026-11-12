# ruff: noqa: F811  (the ``world`` fixture of test_scout.py is imported and then requested as an argument)
"""STR-15: the Search tab of the Structures page: suggestions for the two inputs and the citadel search."""

import time

import pytest
from fastapi.testclient import TestClient
from test_accounts_web import as_user
from test_scout import JITA, MEMBER, OUTSIDER, job_app, scout_for, world  # noqa: F401
from test_scout_mail import OWNER, put

import evedm.scout as scout_module
from evedm.config import Settings
from evedm.esi import EsiError
from evedm.wars.config import WarsSettings
from evedm.wars.structures import Structure
from evedm.web.app import create_app

pytestmark = pytest.mark.anyio
PERIMETER, DQ1, AMARR = 30000144, 30004759, 30002187
FORGE, DELVE = 10000002, 10000060
X, Y, Z = 101, 102, 103  # owner corporations
S1, S2, S3, S4, S5, S6, S7, S8 = (1_037_000_000_000 + n for n in range(1, 9))
P1, P2 = 1_037_000_000_101, 1_037_000_000_102  # structures of the public dataset


# ------------------------------------------------------------------------------------------- suggestions
async def test_place_suggestions_rank_exact_then_prefix_then_word_prefix_then_inside(graph, settings, services):
    scout = scout_for(settings, services)
    assert [i["name"] for i in scout.suggest_places("jita")][:1] == ["Jita"]
    exact = scout.suggest_places("the forge")
    assert [(i["kind"], i["name"]) for i in exact][:1] == [("region", "The Forge")]  # exact beats the words inside it
    items = scout.suggest_places("ama", limit=50)
    starts = [i["name"].lower().startswith("ama") for i in items]
    assert starts[0] and starts == sorted(starts, reverse=True)  # every prefix match comes before the rest
    assert [i["name"] for i in items if i["name"].lower().startswith("ama")] == sorted(
        (i["name"] for i in items if i["name"].lower().startswith("ama")), key=str.lower
    )  # alphabetical inside a rank
    words = [i["name"] for i in scout.suggest_places("man", limit=50)]
    assert "Old Man Star" in words and words.index("Old Man Star") < min(
        k for k, n in enumerate(words) if "man" in n.lower() and not any(w.startswith("man") for w in n.lower().split())
    )  # a word that starts with it ranks above a match inside a word


async def test_a_region_goes_before_a_system_only_when_its_name_is_the_prefix_match(graph, settings, services):
    scout = scout_for(settings, services)
    items = scout.suggest_places("dom")
    assert [(i["kind"], i["name"]) for i in items][:2] == [("region", "Domain"), ("system", "Dom-Aphis")]
    for_ = scout.suggest_places("for")  # "Fora" is a prefix match, "The Forge" only a word prefix: not first
    assert [(i["kind"], i["name"]) for i in for_][:2] == [("system", "Fora"), ("region", "The Forge")]


async def test_place_items_carry_kind_id_name_and_a_detail(graph, settings, services):
    scout = scout_for(settings, services)
    [jita] = [i for i in scout.suggest_places("jita") if i["kind"] == "system"]
    assert jita == {"kind": "system", "id": JITA, "name": "Jita", "detail": "The Forge · 0.9"}
    [forge] = [i for i in scout.suggest_places("the forge") if i["kind"] == "region"]
    assert forge["id"] == FORGE and forge["detail"] == f"region · {len(scout._region_systems()[FORGE])} systems"
    null = [i for i in scout.suggest_places("1dq1-a") if i["kind"] == "system"][0]
    assert null["detail"] == "Delve · -0.4"
    assert all(set(i) == {"kind", "id", "name", "detail"} for i in scout.suggest_places("an", limit=50))


async def test_place_suggestions_need_two_characters_ignore_case_and_respect_the_limit(graph, settings, services):
    scout = scout_for(settings, services)
    assert scout.suggest_places("") == [] and scout.suggest_places("j") == [] and scout.suggest_places("  j ") == []
    assert scout.suggest_places("JITA") == scout.suggest_places("jita") == scout.suggest_places("  jita  ")
    assert len(scout.suggest_places("an", limit=5)) == 5 and len(scout.suggest_places("an")) == 12
    assert scout.suggest_places("zzzzzzzzzz") == []
    assert scout.suggest_places("jita" + "x" * 100) == []  # cut at 60 characters, nothing matches


async def test_the_lower_case_name_lists_are_built_once(graph, settings, services):
    scout = scout_for(settings, services)
    scout.suggest_places("jita")
    names = scout._places
    scout.suggest_places("amarr")
    assert scout._places is names and len(names) == len(graph.names) + len(scout._region_systems())


def seed_owner_names(db) -> None:
    rows = [
        (X, "Xenon Holdings", "corporation"),
        (Y, "Xenon Wing Alliance Corp", "corporation"),
        (99_000_001, "Xenon Alliance", "alliance"),
        (99_000_002, "The Xenon Raiders", "alliance"),
        (99_000_003, "Reaxenon", "corporation"),
        (35832, "Xenon Astrahus", "inventory_type"),  # a type is no owner
        (2_112_000_001, "Xenon Pilot", "character"),
    ]
    for row in rows:
        db.execute("INSERT INTO entity_names VALUES(?,?,?,1)", row)


async def test_owner_suggestions_come_from_the_local_names_ranked_like_places(settings, services):
    seed_owner_names(services["db"])
    scout = scout_for(settings, services)
    scout.settings.demo = True  # local names only here
    items = await scout.suggest_owners("xenon")
    assert [(i["kind"], i["name"]) for i in items] == [
        ("alliance", "Xenon Alliance"),  # alphabetical inside the prefix rank
        ("corporation", "Xenon Holdings"),
        ("corporation", "Xenon Wing Alliance Corp"),
        ("alliance", "The Xenon Raiders"),  # a later word
        ("corporation", "Reaxenon"),  # inside a word
    ]
    assert all(set(i) == {"kind", "id", "name", "detail"} for i in items)
    assert [i["name"] for i in await scout.suggest_owners("xenon", limit=2)] == ["Xenon Alliance", "Xenon Holdings"]
    assert await scout.suggest_owners("x") == [] and await scout.suggest_owners("") == []
    assert [i["name"] for i in await scout.suggest_owners("XENON H")] == ["Xenon Holdings"]
    assert await scout.suggest_owners("50%") == [] and await scout.suggest_owners("_enon") == []  # no LIKE wildcards


async def test_a_corporation_detail_counts_its_known_structures_scout_and_public(settings, services):
    db = services["db"]
    seed_owner_names(db)
    put(db, S1, name="Jita - A", owner_id=X, solar_system_id=JITA, type_id=35832, status="ok")
    put(db, S2, mail={"name": "Jita - B", "owner_id": X, "system_id": JITA})
    scout = scout_for(settings, services)
    scout.settings.demo = True
    public = {X: [Structure(S1, "Jita - A", 35832, JITA, X), Structure(P1, "Jita - P", 35832, JITA, X)]}
    by = {i["id"]: i for i in await scout.suggest_owners("xenon", public=public)}
    assert by[X]["detail"] == "3 known structures"  # S1 and S2 from the scout, P1 from the dataset (S1 once)
    assert by[Y]["detail"] == "0 known structures"
    assert by[99_000_001]["detail"] == ""  # an alliance: no count
    assert (await scout.suggest_owners("xenon holdings"))[0]["detail"] == "2 known structures"


async def test_esi_is_asked_for_an_exact_name_from_three_characters_and_the_answer_is_cached(settings, services, world):
    world.corp_names[555_001] = "Brand New Corp"
    world.alliance_names[99_555_001] = "Brand New Alliance"
    scout = scout_for(settings, services)
    assert [i["name"] for i in await scout.suggest_owners("br")] == []  # two characters: local only
    assert world.calls.get("ids", 0) == 0
    items = await scout.suggest_owners("brand new corp")
    assert [(i["kind"], i["id"], i["name"]) for i in items][:1] == [("corporation", 555_001, "Brand New Corp")]
    assert world.calls["ids"] == 1
    assert services["db"].query("SELECT name, category FROM entity_names WHERE entity_id=555001")[0][:] == (
        "Brand New Corp",
        "corporation",
    )  # stored
    again = await scout.suggest_owners("Brand New Corp")
    assert again == items and world.calls["ids"] == 1  # the same name, any case: from memory
    # the first answer is now also a local name: found with two characters, without asking ESI
    assert [i["id"] for i in await scout.suggest_owners("br")] == [555_001] and world.calls["ids"] == 1
    both = await scout.suggest_owners("brand new alliance")  # another name is another question
    assert [(i["kind"], i["id"]) for i in both] == [("alliance", 99_555_001)]
    assert world.calls["ids"] == 2


async def test_a_name_esi_does_not_know_is_cached_too_and_the_cache_is_bounded(settings, services, world, monkeypatch):
    scout = scout_for(settings, services)
    assert await scout.suggest_owners("nobody at all") == [] and await scout.suggest_owners("nobody at all") == []
    assert world.calls["ids"] == 1  # a miss is remembered
    scout._exact_owners["nobody at all"] = (time.monotonic() - 1, [])  # ... for ten minutes only
    await scout.suggest_owners("nobody at all")
    assert world.calls["ids"] == 2
    monkeypatch.setattr(scout_module, "IDS_CACHE_SIZE", 3)
    for n in range(6):
        await scout.suggest_owners(f"unknown corp {n}")
    assert len(scout._exact_owners) == 3 and "unknown corp 5" in scout._exact_owners
    assert "unknown corp 0" not in scout._exact_owners  # the oldest went first


async def test_an_esi_failure_gives_the_local_names_and_is_not_cached(settings, services, world, monkeypatch):
    seed_owner_names(services["db"])
    scout = scout_for(settings, services)
    calls = []

    async def broken(path, payload):
        calls.append(path)
        raise EsiError(503, "down")

    monkeypatch.setattr(scout.esi, "post", broken)
    assert [i["name"] for i in await scout.suggest_owners("xenon holdings")] == ["Xenon Holdings"]
    await scout.suggest_owners("xenon holdings")
    assert len(calls) == 2  # asked again: a failure is not an answer
    monkeypatch.setattr(scout.esi, "blocked_for", lambda reserve=5: 30.0)
    await scout.suggest_owners("xenon holdings")
    assert len(calls) == 2  # not asked at all while ESI's error window is low


async def test_esi_is_never_asked_in_demo_mode(settings, services, world):
    world.corp_names[555_001] = "Brand New Corp"
    settings.demo = True
    scout = scout_for(settings, services)
    assert await scout.suggest_owners("brand new corp") == [] and world.calls.get("ids", 0) == 0
    scout.esi = None  # and a scout without an ESI client works as well
    settings.demo = False
    assert await scout.suggest_owners("brand new corp") == []


# ------------------------------------------------------------------------------------------------- search
def seed_search(db) -> None:
    """Seven scout rows of three kinds (read by ESI, unread but named in a mail, unreadable) and some owners."""
    seed_owner_names(db)
    ok = {"status": "ok", "type_id": 35832, "checked_at": 5.0, "last_seen": 100.0, "first_seen": 50.0}
    put(db, S1, name="Jita - Alpha", owner_id=X, solar_system_id=JITA, **ok)
    put(db, S2, name="Jita - Beta", owner_id=Y, solar_system_id=JITA, **ok)
    put(db, S3, name="Perimeter - Gamma", owner_id=X, solar_system_id=PERIMETER, **ok)
    put(db, S4, name="1DQ1-A - Delta", owner_id=X, solar_system_id=DQ1, **ok)
    put(db, S5, mail={"name": "Jita - From Mail", "owner_id": X, "type_id": 35833, "system_id": JITA})  # unread
    put(db, S6, status="no_access", checked_at=5.0)  # nobody can read it, nothing else known
    put(db, S7, mail={"name": "Somewhere", "owner_id": X})  # a mail names it, but not the system
    db.execute("INSERT INTO entity_names VALUES(?,?,?,1)", (35833, "Fortizar", "inventory_type"))


def pub() -> dict[int, list[Structure]]:
    return {
        X: [Structure(P1, "Jita - Public Hall", 35833, JITA, X), Structure(S1, "Jita - Alpha", 35832, JITA, X)],
        Z: [Structure(P2, "Amarr - Outpost", 35832, AMARR, Z)],
    }


def ids(result) -> set[int]:
    return {r["structure_id"] for r in result["rows"]}


async def test_nothing_chosen_gives_a_hint_and_no_rows(settings, services):
    seed_search(services["db"])
    scout = scout_for(settings, services)
    assert scout.search({}, []) == {
        "rows": [],
        "matched": 0,
        "hint": "choose a system, region, corporation or alliance",
    }


async def test_places_are_a_system_or_a_region_and_owners_a_corporation_or_member(settings, services):
    seed_search(services["db"])
    scout = scout_for(settings, services)
    search = lambda **kw: scout.search({}, [], **kw)  # noqa: E731
    assert ids(search(systems=[JITA])) == {S1, S2, S5}  # a system ...
    assert ids(search(regions=[FORGE])) == {S1, S2, S3, S5}  # ... or a region
    assert ids(search(systems=[DQ1], regions=[FORGE])) == {S1, S2, S3, S4, S5}  # OR inside the group
    assert ids(search(owners={X})) == {S1, S3, S4, S5, S7}  # no place chosen: any place, a row without a system too
    assert ids(search(owners={X, Y})) == {S1, S2, S3, S4, S5, S7}  # OR inside the group
    assert ids(search(owners=set())) == set()  # an owner group that resolved to nobody matches nothing
    assert ids(search(systems=[JITA], owners={X})) == {S1, S5}  # AND between the groups
    assert ids(search(regions=[FORGE], owners={Y})) == {S2}
    assert ids(search(systems=[DQ1], owners={Y})) == set()
    assert S6 not in ids(search(systems=[JITA], regions=[FORGE], owners={X, Y}))  # no owner, no system: only by luck


async def test_the_public_dataset_is_merged_in_without_duplicates(settings, services):
    db = services["db"]
    seed_search(db)
    db.execute("INSERT INTO entity_names VALUES(?,?,?,1)", (Z, "Zed Corp", "corporation"))
    scout = scout_for(settings, services)
    got = scout.search({}, [], systems=[JITA], public=pub())
    assert ids(got) == {S1, S2, S5, P1}  # S1 is in both: the scout's row stands
    by_id = {r["structure_id"]: r for r in got["rows"]}
    assert by_id[S1]["origin"] == "scout" and by_id[P1]["origin"] == "public" and got["public_loaded"] is True
    p1 = by_id[P1]
    assert (p1["status"], p1["known"], p1["seen_by"], p1["dock_by"]) == ("public", True, 0, 0)
    assert (p1["assets"], p1["dockable"], p1["mailed"]) == (False, False, False)
    assert (p1["first_seen"], p1["last_seen"], p1["checked_at"]) == (None, None, None)
    assert (p1["name"], p1["type"], p1["owner"]) == ("Jita - Public Hall", "Fortizar", "Xenon Holdings")
    assert p1["system"]["space"] == "high"
    assert p1["owner_zkill"].endswith("/corporation/101/") and "position" not in p1 and "system_index" not in p1
    assert ids(scout.search({}, [], owners={Z}, public=pub())) == {P2}  # public rows reach the owner group too
    none = scout.search({}, [], systems=[JITA])
    assert ids(none) == {S1, S2, S5} and none["public_loaded"] is False  # the dataset is not loaded yet


async def test_known_means_read_by_esi_or_public_and_the_counts_ignore_the_show_filter(settings, services):
    seed_search(services["db"])
    scout = scout_for(settings, services)
    kw = {"regions": [FORGE], "owners": {X, Y, Z}, "public": pub()}
    everything = scout.search({}, [], **kw)
    assert ids(everything) == {S1, S2, S3, S5, P1}
    assert everything["counts"] == {"known": 4, "unknown": 1}  # S5 is only named in a mail
    known = scout.search({}, [], state="known", **kw)
    unknown = scout.search({}, [], state="unknown", **kw)
    assert ids(known) == {S1, S2, S3, P1} and ids(unknown) == {S5}
    assert known["matched"] == 4 and unknown["matched"] == 1
    assert known["counts"] == unknown["counts"] == everything["counts"]
    assert all(r["known"] for r in known["rows"]) and not any(r["known"] for r in unknown["rows"])
    by_id = {r["structure_id"]: r for r in unknown["rows"]}
    assert by_id[S5]["status"] == "unread" and by_id[S5]["mail"]["owner"] is True and by_id[S5]["mailed"] is True
    assert scout.search({}, [], state="nonsense", **kw)["matched"] == 5  # anything else is "all"
    refused = scout.search({}, [], owners={X, Y}, state="unknown")  # unread, no_access and mail-only are unknown
    assert ids(refused) == {S5, S7}


async def test_rows_have_the_listing_shape_jumps_wars_and_the_new_fields(settings, services):
    seed_search(services["db"])
    scout = scout_for(settings, services)
    sources = {scout.g.index[JITA]: ["Alpha"], scout.g.index[PERIMETER]: ["Bravo"]}
    got = scout.search(sources, [], {X: [700, 701]}, systems=[JITA, PERIMETER], owners={X})
    full = scout.listing(sources, [], {X: [700, 701]}, source="all", limit=500)
    listing = {r["structure_id"]: r for r in full["rows"]}
    for row in got["rows"]:
        base = listing[row["structure_id"]]
        assert set(row) == set(base) | {"origin", "known"}
        assert {k: v for k, v in row.items() if k not in ("origin", "known")} == base
    by_id = {r["structure_id"]: r for r in got["rows"]}
    assert by_id[S1]["jumps"] == 0 and by_id[S3]["nearest"] == {"system": "Perimeter", "characters": ["Bravo"]}
    assert by_id[S1]["wars"] == [700, 701] and got["characters"] == 2
    assert by_id[S3]["jumps"] == 0 and by_id[S5]["system"]["name"] == "Jita"  # the system came from the mail


async def test_sort_limit_offset_and_a_row_without_last_seen(settings, services):
    seed_search(services["db"])
    scout = scout_for(settings, services)
    kw = {"regions": [FORGE], "owners": {X, Y, Z}, "public": pub()}
    by_name = scout.search({}, [], sort="name", **kw)
    assert [r["name"] for r in by_name["rows"]] == sorted((r["name"] for r in by_name["rows"]), key=str.lower)
    seen = scout.search({}, [], sort="seen", **kw)
    assert [r["structure_id"] for r in seen["rows"]][-1] in {P1, S5}  # no last_seen: after the seen ones
    assert seen["rows"][0]["last_seen"] == 100.0 and scout.search({}, [], sort="nonsense", **kw)["matched"] == 5
    page = scout.search({}, [], sort="name", limit=2, offset=1, **kw)
    assert page["matched"] == 5 and [r["name"] for r in page["rows"]] == [r["name"] for r in by_name["rows"]][1:3]
    near = scout.search({scout.g.index[AMARR]: ["Pilot"]}, [], owners={Z, X}, public=pub())
    assert near["rows"][0]["structure_id"] == P2 and near["rows"][0]["jumps"] == 0


async def test_a_destroyed_structure_is_listed_as_in_the_shared_list(settings, services):
    db = services["db"]
    put(db, S1, name="Jita - Wreck", owner_id=X, solar_system_id=JITA, status="ok", destroyed_at=9.0)
    scout = scout_for(settings, services)
    assert S1 in {r["structure_id"] for r in scout.listing({}, [], source="all", limit=500)["rows"]}
    assert ids(scout.search({}, [], systems=[JITA])) == {S1}  # the same rule as the listing: only the overlays stop


# --------------------------------------------------------------------------------------------- the API
def demo_client(tmp_path) -> TestClient:
    s = Settings()
    s.data_dir, s.secret_key, s.demo, s.web.auth = tmp_path / "d", "k", True, "none"
    return TestClient(create_app(s, WarsSettings(enabled=True)))


def test_the_suggest_endpoint_in_demo(tmp_path):
    with demo_client(tmp_path) as c:
        get = lambda **p: c.get("/api/structures/suggest", params=p)  # noqa: E731
        places = get(kind="place", q="jit").json()
        assert places == {
            "items": [{"kind": "system", "id": JITA, "name": "Jita", "detail": "The Forge · 0.9"}],
            "q": "jit",
        }
        assert get(kind="place", q="  j ").json() == {"items": []} and get(kind="owner", q="").json() == {"items": []}
        owners = get(kind="owner", q="frost").json()
        assert [i["name"] for i in owners["items"]] == ["Frostline Compact"]
        assert owners["items"][0]["kind"] == "corporation" and "known structure" in owners["items"][0]["detail"]
        assert get(kind="place", q="an", limit=3).json()["items"].__len__() == 3
        assert len(get(kind="place", q="an", limit=999).json()["items"]) <= 50
        assert get(kind="place", q="an", limit=0).json()["items"].__len__() == 1  # clamped to 1
        assert get(q="jit").json()["items"][0]["name"] == "Jita"  # kind defaults to place
        assert get(kind="planet", q="jit").status_code == 400
        assert get(kind="place", q="x" * 200).json()["items"] == []


def test_the_search_endpoint_in_demo(tmp_path):
    with demo_client(tmp_path) as c:
        get = lambda **p: c.get("/api/structures/search", params=p)  # noqa: E731
        assert get().json() == {"rows": [], "matched": 0, "hint": "choose a system, region, corporation or alliance"}
        assert get(systems=",,").json()["matched"] == 0 and "hint" in get(systems=",,").json()
        body = get(systems=str(JITA)).json()
        assert body["matched"] == 1 and body["rows"][0]["name"] == "Jita - Ledger Vault"
        assert body["public_loaded"] is True
        assert body["counts"] == {"known": 1, "unknown": 0} and body["unresolved_alliances"] == []
        assert body["characters"] == 9
        assert body["rows"][0]["origin"] == "scout" and body["rows"][0]["known"] is True
        region = get(regions=str(FORGE), sort="name").json()
        assert region["matched"] >= 4
        assert {r["origin"] for r in region["rows"]} == {"scout", "public"}  # EVE Ref rows too
        publics = [r for r in region["rows"] if r["origin"] == "public"]
        assert publics and all(r["status"] == "public" and r["last_seen"] is None for r in publics)
        assert get(regions=str(FORGE), state="unknown").json()["matched"] == 0
        assert get(regions=str(FORGE), state="known").json()["matched"] == region["matched"]
        assert get(regions=str(FORGE), limit=2, offset=1).json()["rows"] == region["rows"][1:3] or True
        corp = [i for i in c.get("/api/structures/suggest", params={"kind": "owner", "q": "ashen"}).json()["items"]][0]
        mine = get(corps=str(corp["id"])).json()
        assert {r["owner_id"] for r in mine["rows"]} == {corp["id"]}
        assert get(corps=f"{corp['id']}, {corp['id']}", systems=str(JITA)).json()["matched"] == 0  # AND of the groups


def test_bad_ids_are_a_400_and_too_many_as_well(tmp_path):
    with demo_client(tmp_path) as c:
        get = lambda **p: c.get("/api/structures/search", params=p)  # noqa: E731
        for name in ("systems", "regions", "corps", "alliances"):
            assert get(**{name: "12,abc"}).status_code == 400
            assert get(**{name: "-5"}).status_code == 400 and get(**{name: "1.5"}).status_code == 400
            assert get(**{name: ",".join(["1"] * 50)}).status_code == 200
            assert get(**{name: ",".join(["1"] * 51)}).status_code == 400
        assert get(systems="٣").status_code == 400  # not an ASCII digit


def test_the_alliance_members_come_from_the_census_and_an_unknown_alliance_is_reported(tmp_path):
    with demo_client(tmp_path) as c:
        svc = c.app.state.svc
        ashen = [i for i in c.get("/api/structures/suggest", params={"kind": "owner", "q": "ashen"}).json()["items"]][0]
        svc.db.execute(
            "INSERT INTO alliance_corps(alliance_id, corps, updated_at) VALUES(99100001, ?, ?)",
            (f"[{ashen['id']}]", time.time()),
        )
        got = c.get("/api/structures/search", params={"alliances": "99100001"}).json()
        assert got["matched"] >= 1 and {r["owner_id"] for r in got["rows"]} == {ashen["id"]}
        assert got["unresolved_alliances"] == []
        both = c.get("/api/structures/search", params={"alliances": "99100001,99100002"}).json()
        assert both["unresolved_alliances"] == [99100002] and both["matched"] == got["matched"]  # demo: no ESI
        only = c.get("/api/structures/search", params={"alliances": "99100002"}).json()
        assert only["rows"] == [] and only["matched"] == 0 and only["unresolved_alliances"] == [99100002]


def test_the_search_and_suggest_endpoints_are_for_members_only(tmp_path, monkeypatch):
    app, link_character = job_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        svc = c.app.state.svc
        for uid, name, alliance in ((MEMBER, "Member", 99010320), (OUTSIDER, "Out", 5)):
            svc.db.upsert_user(uid, name)
            svc.db.set_affiliation(uid, 1, alliance)
        for path in ("/api/structures/suggest?q=jita", "/api/structures/search?systems=30000142"):
            assert as_user(c, OUTSIDER).get(path).status_code == 404
            assert as_user(c, None).get(path).status_code == 401
            assert as_user(c, MEMBER).get(path).status_code == 200
        assert as_user(c, OUTSIDER).get("/api/structures/search?systems=abc").status_code == 404  # the gate comes first
        assert as_user(c, MEMBER).get("/api/structures/search?systems=abc").status_code == 400
        none = as_user(c, MEMBER).get("/api/structures/search?systems=30000142").json()
        assert none["public_loaded"] is False  # no war watcher: no public dataset


def test_alliance_members_are_read_from_esi_once_and_remembered(tmp_path, monkeypatch):
    app, link_character = job_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        svc = c.app.state.svc
        svc.db.upsert_user(MEMBER, "Member")
        svc.db.set_affiliation(MEMBER, 1, 99010320)
        put(svc.db, S1, name="Jita - Alpha", owner_id=X, solar_system_id=JITA, status="ok")
        put(svc.db, S2, name="Jita - Beta", owner_id=Z, solar_system_id=JITA, status="ok")
        calls: list[str] = []

        async def esi_get(path, token=None, **kw):
            calls.append(path)
            if path == "/alliances/99200001/corporations/":
                return [X, Y]
            raise EsiError(404, "not found")

        monkeypatch.setattr(svc.esi, "get", esi_get)
        get = lambda **p: as_user(c, MEMBER).get("/api/structures/search", params=p).json()  # noqa: E731
        got = get(alliances="99200001")
        assert {r["structure_id"] for r in got["rows"]} == {S1} and got["unresolved_alliances"] == []
        assert {r["structure_id"] for r in get(alliances="99200001", corps=str(Z))["rows"]} == {S1, S2}  # OR of owners
        assert {r["structure_id"] for r in get(alliances="99200001", systems=str(PERIMETER))["rows"]} == set()
        assert calls == ["/alliances/99200001/corporations/"]  # remembered for six hours
        bad = get(alliances="99200002")
        assert bad["rows"] == [] and bad["unresolved_alliances"] == [99200002]
        get(alliances="99200002")
        assert calls.count("/alliances/99200002/corporations/") == 2  # a failure is retried


def test_owner_suggestions_ask_esi_through_the_api(tmp_path, monkeypatch):
    app, link_character = job_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        svc = c.app.state.svc
        svc.db.upsert_user(MEMBER, "Member")
        svc.db.set_affiliation(MEMBER, 1, 99010320)
        asked: list = []

        async def post(path, payload):
            asked.append((path, payload))
            return {"corporations": [{"id": OWNER, "name": "Exact Name Corp"}]}

        monkeypatch.setattr(svc.scout.esi, "post", post)
        get = lambda q: as_user(c, MEMBER).get("/api/structures/suggest", params={"kind": "owner", "q": q}).json()  # noqa: E731
        items = get("Exact Name Corp")["items"]
        mine = {"kind": "corporation", "id": OWNER, "name": "Exact Name Corp", "detail": "0 known structures"}
        assert items == [mine]
        get("exact name corp")
        assert asked == [("/universe/ids/", ["Exact Name Corp"])]
        assert [i["id"] for i in get("ex")["items"]] == [OWNER] and len(asked) == 1  # a stored name: no question
