import gzip
import io
import json
import zipfile

import pytest

from evedm.universe import (
    GateGraph,
    RawUniverse,
    build_from_jsonl_zip,
    security_class,
    security_display,
    write_universe,
)


def test_bundled_universe_loads(graph):
    assert len(graph.ids) > 8000  # k-space + w-space
    assert graph.edge_count > 6800
    assert graph.find("jita") == graph.find("Jita")
    assert graph.ships[670] == "Capsule"


@pytest.mark.parametrize(
    ("raw", "shown", "cls"),
    [
        (0.945, 0.9, "high"),
        (0.45, 0.5, "high"),
        (0.449, 0.4, "low"),
        (0.04, 0.1, "low"),
        (0.0, 0.0, "null"),
        (-0.04, 0.0, "null"),
        (-0.46, -0.5, "null"),
        (1.0, 1.0, "high"),
    ],
)
def test_security_display_matches_game_rounding(raw, shown, cls):
    assert security_display(raw) == pytest.approx(shown)
    assert security_class(raw) == cls


def test_shortest_routes(graph):
    tama = graph.find("Tama")
    parent = graph.bfs(tama)
    jita = graph.route(parent, graph.find("Jita"))
    assert [graph.names[i] for i in jita] == ["Jita", "Ikuchi", "Tunttaras", "Nourvukaiken", "Tama"]
    assert graph.route(parent, tama) == [tama]
    perimeter = graph.route(graph.bfs(graph.find("Jita")), graph.find("Perimeter"))
    assert len(perimeter) - 1 == 1


def test_route_summary_counts_security_classes(graph):
    parent = graph.bfs(graph.find("Tama"))
    path = graph.route(parent, graph.find("Jita"))
    assert graph.route_summary(path) == {"high": 4, "low": 1, "null": 0}


def test_wormhole_and_pochven_are_unreachable(graph):
    parent = graph.bfs(graph.find("Jita"))
    assert graph.route(parent, graph.find("Thera")) is None
    pochven = [i for i, r in enumerate(graph.region_of) if graph.regions[r] == "Pochven"]
    assert pochven and all(graph.route(parent, i) is None for i in pochven)


def test_avoided_system_is_not_used_for_transit_but_can_be_origin(graph):
    zarzakh = graph.find("Zarzakh")
    target = graph.find("Turnur")
    direct = graph.bfs(target)
    avoided = graph.bfs(target, [zarzakh])
    # Zarzakh itself stays reachable (as an origin) ...
    assert graph.route(avoided, zarzakh) is not None
    # ... but no route passes through it
    for i in range(len(graph.ids)):
        path = graph.route(avoided, i)
        if path and len(path) > 2:
            assert zarzakh not in path[1:-1]
    assert graph.route(direct, graph.find("Alsavoinon")) is not None


def test_bfs_is_fast(graph):
    import time

    t0 = time.perf_counter()
    for _ in range(20):
        graph.bfs(graph.find("Jita"))
    assert (time.perf_counter() - t0) / 20 < 0.05  # NFR-02: <= 50 ms


def test_map_payload_contains_kspace_only(graph):
    payload, etag = graph.map_payload()
    doc = json.loads(gzip.decompress(payload))
    assert etag.startswith('"')
    assert all(i < 31000000 for i in doc["ids"])
    assert len(doc["systems"]) == len(doc["ids"])
    assert max(doc["edges"]) < len(doc["ids"])
    assert graph.map_payload()[1] == etag  # cached


def _jsonl(rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def test_build_from_current_sde_jsonl(tmp_path):
    """Synthetic SDE zip in the current format; also checks the north-up orientation fix."""
    regions = [
        {"_key": 10000002, "name": {"en": "The Forge"}},
        {"_key": 10000043, "name": {"en": "Domain"}},
        {"_key": 10000032, "name": {"en": "Sinq Laison"}},
        {"_key": 10000030, "name": {"en": "Heimatar"}},
    ]

    def system(sid, name, region, x, z, sec=0.9):
        return {
            "_key": sid,
            "name": {"en": name},
            "regionID": region,
            "securityStatus": sec,
            "position": {"x": x, "y": 0, "z": z},
        }

    # z is inverted on purpose (The Forge "south" of Domain): the builder must flip it
    systems = [
        system(30000001, "Alpha", 10000002, 0, -1e17),
        system(30000002, "Beta", 10000043, 0, 1e17),
        system(30000003, "Gamma", 10000032, -1e17, 0, 0.3),
        system(30000004, "Delta", 10000030, 1e17, 0, -0.2),
        system(31000001, "J000001", 11000001, 5e17, 5e17, -1.0),
    ]
    gates = [
        {"_key": 50000001, "solarSystemID": 30000001, "destination": {"solarSystemID": 30000002}},
        {"_key": 50000002, "solarSystemID": 30000002, "destination": {"solarSystemID": 30000001}},
        {"_key": 50000003, "solarSystemID": 30000002, "destination": {"solarSystemID": 30000003}},
    ]
    groups = [{"_key": 25, "categoryID": 6, "name": {"en": "Frigate"}}, {"_key": 1, "categoryID": 1}]
    types = [
        {"_key": 587, "groupID": 25, "name": {"en": "Rifter"}},
        {"_key": 34, "groupID": 18, "name": {"en": "Trit"}},
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mapRegions.jsonl", _jsonl(regions))
        zf.writestr("mapSolarSystems.jsonl", _jsonl(systems))
        zf.writestr("mapStargates.jsonl", _jsonl(gates))
        zf.writestr("groups.jsonl", _jsonl(groups))
        zf.writestr("types.jsonl", _jsonl(types))
    zpath = tmp_path / "sde.zip"
    zpath.write_bytes(buf.getvalue())
    raw = build_from_jsonl_zip(zpath, build="3100000")
    assert len(raw.systems) == 5 and raw.edges == {(30000001, 30000002), (30000002, 30000003)}
    assert raw.ships == {587: "Rifter"}
    out = tmp_path / "u.json.gz"
    write_universe(raw, out)
    g = GateGraph.load(out)
    alpha, beta = g.find("Alpha"), g.find("Beta")
    assert g.my[alpha] > g.my[beta]  # The Forge ends up north of Domain
    assert g.route(g.bfs(alpha), g.find("Gamma")) == [g.find("Gamma"), beta, alpha]
    assert g.build == "3100000"


def test_write_universe_is_deterministic(tmp_path):
    raw = RawUniverse(
        {30000001: {"name": "A", "region": 1, "sec": 0.5, "x": 1.0, "y": 0.0, "z": 2.0}},
        set(),
        {1: "R"},
        {},
        "test",
        "b1",
    )
    a, b = tmp_path / "a.gz", tmp_path / "b.gz"
    write_universe(raw, a)
    write_universe(raw, b)
    da, db = json.loads(gzip.decompress(a.read_bytes())), json.loads(gzip.decompress(b.read_bytes()))
    da.pop("generated_at"), db.pop("generated_at")
    assert da == db


def test_rejects_unknown_format():
    with pytest.raises(ValueError):
        GateGraph({"format": 99})
