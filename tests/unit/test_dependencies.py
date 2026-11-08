"""Dependency validation: every third-party import is in requirements.txt, pins are exact and stable."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from evedm.config import Settings
from evedm.selfcheck import RUNTIME_DISTRIBUTIONS, check

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "evedm"
PRE_RELEASE = re.compile(r"(a|b|rc|dev|alpha|beta|pre)\d*", re.I)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        spec = line.split(";", 1)[0].strip()
        name, _, version = spec.partition("==")
        pins[norm(name)] = version
    return pins


def third_party_imports() -> set[str]:
    mods: set[str] = set()
    for py in SRC.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in sys.stdlib_module_names and m != "evedm"}


def test_every_requirement_is_an_exact_stable_pin():
    pins = requirements()
    assert pins, "requirements.txt is empty"
    for name, version in pins.items():
        assert version, f"{name} is not pinned with =="
        assert not PRE_RELEASE.search(version.split(".")[-1]), f"{name}=={version} is a pre-release"


def test_every_third_party_import_is_in_requirements():
    pins = requirements()
    owners = metadata.packages_distributions()
    missing = []
    for module in sorted(third_party_imports()):
        dists = owners.get(module, [module])
        if not any(norm(d) in pins for d in dists):
            missing.append(f"{module} (from {dists})")
    assert not missing, f"imports not covered by requirements.txt: {missing}"


def test_pyproject_dependencies_are_pinned_in_requirements():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = requirements()
    for dep in pyproject["project"]["dependencies"]:
        name = norm(re.split(r"[<>=!~;\s\[]", dep, maxsplit=1)[0])
        assert name in pins, f"{name} is declared in pyproject.toml but not pinned in requirements.txt"


def test_selfcheck_distribution_list_matches_requirements():
    pins = requirements()
    assert all(norm(d) in pins for d in RUNTIME_DISTRIBUTIONS)


def test_installed_versions_match_pins():
    pins = requirements()
    for dist in RUNTIME_DISTRIBUTIONS:
        installed = metadata.version(dist)
        assert installed == pins[norm(dist)], f"{dist}: installed {installed}, pinned {pins[norm(dist)]}"


def test_selfcheck_resolves_absolute_paths_and_passes(tmp_path):
    s = Settings(data_dir=tmp_path / "relative-or-not")
    report = check(s)
    assert report.ok, report.problems
    for key in ("package", "universe_file", "bundle_schema", "templates_dir", "static_dir", "data_dir", "database"):
        assert Path(report.paths[key]).is_absolute(), key


def test_selfcheck_reports_missing_universe_and_readonly_data(tmp_path):
    s = Settings(data_dir=tmp_path / "ro", universe_file=tmp_path / "missing.json.gz")
    (tmp_path / "ro").mkdir()
    (tmp_path / "ro").chmod(0o500)
    try:
        report = check(s)
    finally:
        (tmp_path / "ro").chmod(0o700)
    assert not report.ok
    assert any("universe_file" in p for p in report.problems)
    import os

    if hasattr(os, "geteuid") and os.geteuid() != 0:  # root can write anywhere
        assert any("not writable" in p for p in report.problems)


@pytest.mark.parametrize("mode", ["wal"])
def test_database_defaults_to_wal_and_checkpoints_on_close(tmp_path, mode):
    from evedm.db import Database

    db = Database(tmp_path / "x.sqlite3")
    assert db.journal_mode == mode
    db.upsert_character(1, "A", "OK", [])
    assert (tmp_path / "x.sqlite3-wal").exists()
    db.close()
    assert not (tmp_path / "x.sqlite3-wal").exists() or (tmp_path / "x.sqlite3-wal").stat().st_size == 0
