"""scripts/build_structure_index.py: crawl, download, merge and write the flat list; evedm.structure_index reads it."""

import bz2
import importlib.util
import io
import json
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from evedm.structure_index import index_path, lookup

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_structure_index.py"
spec = importlib.util.spec_from_file_location("build_structure_index", SCRIPT)
bsi = importlib.util.module_from_spec(spec)
sys.modules["build_structure_index"] = bsi
spec.loader.exec_module(bsi)

S1, S2, S3, S4 = 1_035_000_000_001, 1_035_000_000_002, 1_035_000_000_003, 1_035_000_000_004
JITA, FORGE, KEEPSTAR = 30000142, 10000002, 35834


def test_times_and_stamps_are_normalised():
    assert bsi.norm_time("2026-11-09T04:00:00Z") == "2026-11-09T04:00:00Z"
    assert bsi.norm_time("2026-10-01 10:00:00") == "2026-10-01T10:00:00Z"
    assert bsi.norm_time("2019-03-29") == "2019-03-29T00:00:00Z"
    assert bsi.norm_time("2026-11-09T05:00:00+01:00") == "2026-11-09T04:00:00Z"
    assert bsi.norm_time("1970-01-01T00:00:00Z") is None and bsi.norm_time("") is None and bsi.norm_time("soon") is None
    assert bsi.stamp_from_name("structures-2026-03-31_04-34-43.v2.json.bz2") == "2026-03-31T04:34:43Z"
    assert bsi.stamp_from_name("nothing.json") is None


def rec(sid, source, observed, **fields):
    base = dict.fromkeys(bsi.FIELDS) | {
        "structure_id": sid,
        "kind": "structure",
        "observed": observed,
        "first_seen": None,
    }
    return base | {"source": source} | fields


def test_newest_observation_wins_and_older_ones_fill_the_gaps():
    index: dict = {}
    bsi.merge_into(
        index,
        rec(
            S1,
            "EVE Ref history",
            "2026-01-01T00:00:00Z",
            name="Old name",
            owner_id=7,
            first_seen="2025-05-05T00:00:00Z",
        ),
    )
    bsi.merge_into(
        index,
        rec(
            S1,
            "EVE Ref history",
            "2026-06-01T00:00:00Z",
            name="New name",
            solar_system_id=JITA,
            first_seen="2026-02-02T00:00:00Z",
        ),
    )
    bsi.merge_into(index, rec(S1, "Adam4EVE", None, name="Undated", type_name="Astrahus"))
    got = index[S1]
    assert got["name"] == "New name" and got["observed"] == "2026-06-01T00:00:00Z"
    assert (
        got["owner_id"] == 7 and got["solar_system_id"] == JITA and got["type_name"] == "Astrahus"
    )  # filled from older ones
    assert got["first_seen"] == "2025-05-05T00:00:00Z"  # the earliest sighting
    # an older observation that arrives later does not replace a newer one
    bsi.merge_into(index, rec(S1, "EVE Ref history", "2025-01-01T00:00:00Z", name="Ancient"))
    assert index[S1]["name"] == "New name"
    # same time: the higher-priority source wins
    bsi.merge_into(index, rec(S2, "Adam4EVE extended", "2026-01-01T00:00:00Z", name="Adam"))
    bsi.merge_into(index, rec(S2, "EVE Ref history", "2026-01-01T00:00:00Z", name="Ref"))
    assert index[S2]["name"] == "Ref"


PAGES = {
    "https://x.test/h/": '<a href="../">Parent</a><a href="?C=N;O=D">Name</a><a href="2026-01/">2026-01/</a>'
    '<a href="a%20b.json.bz2">a b</a><a href="https://elsewhere.test/z.json">other site</a>',
    "https://x.test/h/2026-01/": '<a href="/h/">Parent</a><a href="..">up</a>'
    '<a href="deep/">deep/</a><a href="c.json">c</a>',
    "https://x.test/h/2026-01/deep/": '<a href="../">Parent</a><a href="d.tar.xz">d</a>',
}


def test_listing_walks_every_folder_and_ignores_parent_and_sort_links():
    class Fake(bsi.Fetcher):
        def get_bytes(self, url):
            return PAGES[url].encode()

    files = bsi.list_files("https://x.test/h/", Fake())
    assert files == [
        "https://x.test/h/2026-01/c.json",
        "https://x.test/h/2026-01/deep/d.tar.xz",
        "https://x.test/h/a%20b.json.bz2",
    ]
    base = Path("/tmp/evedm-test-base")
    assert bsi.local_path("https://x.test/h/", files[2], base).name == "a b.json.bz2"
    with pytest.raises(ValueError):
        bsi.local_path("https://x.test/h/", "https://x.test/h/../../etc/passwd", base)


class Handler(BaseHTTPRequestHandler):
    calls: dict = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        n = Handler.calls[self.path] = Handler.calls.get(self.path, 0) + 1
        if self.path == "/flaky.bin" and n == 1:
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if self.path == "/missing.bin":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("If-Modified-Since") and self.path == "/latest.json":
            self.send_response(304)
            self.end_headers()
            return
        body = b'{"ok": true}' if self.path == "/latest.json" else b"x" * 3000
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", "Mon, 02 Nov 2026 03:09:00 GMT")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    Handler.calls = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_downloads_are_atomic_resumable_and_conditional(server, tmp_path):
    f = bsi.Fetcher(retries=2, timeout=5)
    dest = tmp_path / "sub" / "flaky.bin"
    assert f.download(f"{server}/flaky.bin", dest) == "downloaded"  # 503 once, then fine
    assert dest.stat().st_size == 3000 and not list(tmp_path.rglob("*.part"))
    assert (
        f.download(f"{server}/flaky.bin", dest) == "skipped" and Handler.calls["/flaky.bin"] == 2
    )  # not fetched again
    latest = tmp_path / "latest.json"
    assert f.download(f"{server}/latest.json", latest, refresh=True) == "downloaded"
    assert f.download(f"{server}/latest.json", latest, refresh=True) == "not-modified"  # If-Modified-Since
    counts = bsi.download_many(
        f, [(f"{server}/missing.bin", tmp_path / "m.bin"), (f"{server}/flaky.bin", dest)], workers=2
    )
    assert counts["failed"] == 1 and counts["skipped"] == 1 and not (tmp_path / "m.bin").exists()


def snapshot(rows):
    return {str(sid): rec for sid, rec in rows.items()}


def make_sources(base: Path) -> None:
    (base / "adam4eve").mkdir(parents=True)
    (base / "adam4eve" / "playerStructure_extended.csv").write_text(
        "structureID;typeID;typeName;solarSystemID;regionID;name;ownerID;ownerName;freeport;firstSeen;lastSeen\n"
        f'{S1};35832;Astrahus;{JITA};{FORGE};"Jita - Adam name";98000001;"Adam Corp";0;2018-01-01;2026-02-01\n'
        f'{S3};35825;Raitaru;{JITA};{FORGE};"Jita - Only Adam";98000002;"Other Corp";1;2019-01-01;2019-02-01\n',
        encoding="utf-8",
    )
    (base / "adam4eve" / "playerStructure_IDs.csv").write_text(
        f'structureID;typeID;solarSystemID;name;ownerID\n{S4};35832;{JITA};"Undated";98000003\n', encoding="utf-8"
    )
    (base / "adam4eve" / "npcStation_ids.csv").write_text(
        'stationID;solarSystemID;stationName\n60003760;30000142;"Jita IV - Moon 4 - Caldari Navy Assembly Plant"\n',
        encoding="utf-8",
    )
    hist = base / "everef" / "history" / "2026"
    (hist / "03").mkdir(parents=True)
    old = snapshot(
        {
            S1: {
                "name": "Jita - Ref old",
                "owner_id": 98000001,
                "solar_system_id": JITA,
                "type_id": 35832,
                "last_structure_get": "2026-03-01T00:00:00Z",
            },
            S2: {"is_gettable_structure": False, "first_seen": "1970-01-01T00:00:00Z"},
        }
    )
    (hist / "structures-2026-03-01_00-00-00.v2.json.bz2").write_bytes(bz2.compress(json.dumps(old).encode()))
    newer = snapshot(
        {
            S1: {
                "name": "Jita - Ref new",
                "owner_id": 98000001,
                "solar_system_id": JITA,
                "type_id": 35832,
                "last_structure_get": "2026-05-01T00:00:00Z",
                "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                "first_seen": "2026-03-01T00:00:00Z",
            },
            S2: {
                "name": "Perimeter - Ref only",
                "owner_id": 98000004,
                "solar_system_id": 30000144,
                "type_id": KEEPSTAR,
            },
        }
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        data = json.dumps(newer).encode()
        info = tarfile.TarInfo("structures-2026-05-02_00-00-00.v2.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    (hist / "03" / "structures-2026-05.tar.xz").write_bytes(buf.getvalue())
    (hist / "broken.json.bz2").write_bytes(b"not bzip2")
    latest = snapshot(
        {
            S1: {
                "name": "Jita - Ref latest",
                "owner_id": 98000001,
                "solar_system_id": JITA,
                "type_id": 35832,
                "last_structure_get": "2026-11-09T04:00:00Z",
            }
        }
    )
    (base / "everef" / "structures-latest.v2.json").write_text(json.dumps(latest), encoding="utf-8")
    (base / "sde").mkdir()
    with zipfile.ZipFile(base / "sde" / "eve-online-static-data-3682781-jsonl.zip", "w") as z:
        z.writestr(
            "mapSolarSystems.jsonl",
            json.dumps({"_key": JITA, "name": {"en": "Jita"}, "regionID": FORGE})
            + "\n"
            + json.dumps({"_key": 30000144, "name": {"en": "Perimeter"}, "regionID": FORGE})
            + "\n",
        )
        z.writestr("mapRegions.jsonl", json.dumps({"_key": FORGE, "name": {"en": "The Forge"}}) + "\n")
        z.writestr(
            "types.jsonl",
            json.dumps({"_key": 35832, "name": {"en": "Astrahus"}})
            + "\n"
            + json.dumps({"_key": 35825, "name": {"en": "Raitaru"}})
            + "\n"
            + json.dumps({"_key": KEEPSTAR, "name": {"en": "Keepstar"}})
            + "\n",
        )
        z.writestr("npcCorporations.jsonl", json.dumps({"_key": 1000125, "name": {"en": "Caldari Navy"}}) + "\n")


def test_build_merges_every_source_into_one_flat_list(tmp_path):
    base = tmp_path / "data" / "structure-sources"
    make_sources(base)
    meta = bsi.build_index(base, tmp_path / "data")
    lines = (tmp_path / "data" / "structure-index.jsonl").read_text(encoding="utf-8").splitlines()
    rows = {r["structure_id"]: r for r in map(json.loads, lines)}
    assert all(line.startswith('{"structure_id":') for line in lines)
    assert [json.loads(line)["structure_id"] for line in lines] == sorted(rows)  # sorted: the reader can stop early
    assert meta["records"] == len(rows) == 5 and meta["stations"] == 1 and meta["sde_build"] == "3682781"
    # S1 is seen by Adam4EVE (Feb), EVE Ref history (Mar, May) and the latest file (Nov): the newest name, one line
    assert rows[S1]["name"] == "Jita - Ref latest" and rows[S1]["source"] == "EVE Ref latest"
    assert rows[S1]["observed"] == "2026-11-09T04:00:00Z" and rows[S1]["first_seen"] == "2018-01-01T00:00:00Z"
    assert rows[S1]["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}  # a gap in the newest record filled from an older one
    assert rows[S1]["owner_name"] == "Adam Corp" and rows[S1]["type_name"] == "Astrahus"
    assert (
        rows[S1]["solar_system_name"] == "Jita"
        and rows[S1]["region_name"] == "The Forge"
        and rows[S1]["region_id"] == FORGE
    )
    # the structure only the history knows, with names completed from the Static Data Export
    assert rows[S2]["name"] == "Perimeter - Ref only" and rows[S2]["solar_system_name"] == "Perimeter"
    assert rows[S2]["type_name"] == "Keepstar" and rows[S2]["source"] == "EVE Ref history"
    assert rows[S3]["name"] == "Jita - Only Adam" and rows[S3]["observed"] == "2019-02-01T00:00:00Z"
    assert rows[S4]["name"] == "Undated" and rows[S4]["observed"] is None
    assert rows[60003760]["kind"] == "station" and rows[60003760]["solar_system_name"] == "Jita"


def test_build_without_any_source_writes_an_empty_list(tmp_path):
    meta = bsi.build_index(tmp_path / "none", tmp_path / "out")
    assert meta["records"] == 0 and (tmp_path / "out" / "structure-index.jsonl").read_text() == ""


def test_reader_returns_only_the_wanted_ids_and_survives_a_missing_file(tmp_path):
    base = tmp_path / "data" / "structure-sources"
    make_sources(base)
    bsi.build_index(base, tmp_path / "data")
    path = index_path(tmp_path / "data")
    got = lookup(path, {S2, S4, 5})
    assert {r["structure_id"] for r in got} == {S2, S4} and next(r for r in got if r["structure_id"] == S2)[
        "name"
    ] == "Perimeter - Ref only"
    assert lookup(path, set()) == [] and lookup(tmp_path / "missing.jsonl", {S1}) == []
    assert index_path(tmp_path, "~/custom.jsonl").name == "custom.jsonl"
    odd = tmp_path / "odd.jsonl"  # lines not written by the build script are parsed
    odd.write_text('{"name": "x", "structure_id": 7}\nnot json\n{"structure_id": 9, "name": "y"}\n', encoding="utf-8")
    assert [r["structure_id"] for r in lookup(odd, {7, 9})] == [7, 9]


def test_a_latest_file_dropped_into_the_data_folder_is_used_too(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "structures-latest.v2.json").write_text(
        json.dumps(
            {
                str(S1): {
                    "name": "Jita - Dropped",
                    "owner_id": 5,
                    "solar_system_id": JITA,
                    "last_structure_get": "2026-11-10T18:41:46Z",
                }
            }
        ),
        encoding="utf-8",
    )
    bsi.build_index(data / "structure-sources", data)
    rows = [json.loads(line) for line in (data / "structure-index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["structure_id"], r["name"], r["source"]) for r in rows] == [(S1, "Jita - Dropped", "EVE Ref latest")]
