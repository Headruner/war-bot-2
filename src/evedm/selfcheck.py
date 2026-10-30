"""Startup and build-time verification: runtime dependencies and fully qualified resource paths."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from .config import Settings

# Runtime distributions the package imports (kept in sync with requirements.txt by tests/unit/test_dependencies.py)
RUNTIME_DISTRIBUTIONS = [
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "httptools",
    "httpx",
    "jinja2",
    "pyyaml",
    "cryptography",
    "pyjwt",
    "jsonschema",
    "python-multipart",
    "itsdangerous",
]
if sys.platform != "win32":
    RUNTIME_DISTRIBUTIONS.append("uvloop")

STATIC_FILES = ["app.css", "map.js", "rank.js", "characters.js", "wars.js", "prefs.js", "favicon.svg"]
TEMPLATES = ["base.html", "index.html", "characters.html", "login.html", "wars.html"]


@dataclass
class CheckReport:
    paths: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "paths": self.paths, "versions": self.versions, "problems": self.problems}


def _package_path(rel: str) -> Path:
    return Path(str(resources.files("evedm").joinpath(rel))).resolve()


def check(settings: Settings, data: bool = True) -> CheckReport:
    """Verify dependencies and resolve every resource path to an absolute, existing location.

    data=False skips the data directory (used at image build time, before a volume is mounted).
    """
    rep = CheckReport()
    rep.versions["python"] = sys.version.split()[0]
    for dist in RUNTIME_DISTRIBUTIONS:
        try:
            rep.versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            rep.problems.append(f"missing dependency: {dist} (see requirements.txt)")

    package_root = _package_path("")
    rep.paths["package"] = str(package_root)
    universe = (
        Path(settings.universe_file).resolve() if settings.universe_file else _package_path("data/universe.json.gz")
    )
    resources_to_check = {
        "universe_file": universe,
        "bundle_schema": _package_path("schema/evedm-characters.v1.yaml"),
        "templates_dir": _package_path("web/templates"),
        "static_dir": _package_path("web/static"),
    }
    resources_to_check.update({f"template:{t}": _package_path(f"web/templates/{t}") for t in TEMPLATES})
    resources_to_check.update({f"static:{f}": _package_path(f"web/static/{f}") for f in STATIC_FILES})
    for name, path in resources_to_check.items():
        if not name.startswith(("template:", "static:")):
            rep.paths[name] = str(path)
        if not path.exists():
            rep.problems.append(f"missing resource {name}: {path}")

    cfg = os.environ.get("EVEDM_CONFIG")
    if cfg:
        cfg_path = Path(cfg).resolve()
        rep.paths["config_file"] = str(cfg_path)
        if not cfg_path.is_file():
            rep.problems.append(f"EVEDM_CONFIG points to a missing file: {cfg_path}")

    from .wars.config import default_path

    wars_cfg = default_path()
    if wars_cfg is not None:
        wars_path = wars_cfg.resolve()
        rep.paths["wars_config"] = str(wars_path) + ("" if wars_path.is_file() else " (absent: wars disabled)")
        if os.environ.get("EVEDM_WARS_CONFIG") and not wars_path.is_file():
            rep.problems.append(f"EVEDM_WARS_CONFIG points to a missing file: {wars_path}")

    if data:
        data_dir = settings.data_dir.resolve()
        rep.paths.update(
            {"data_dir": str(data_dir), "database": str(settings.db_path), "log_dir": str(settings.log_dir)}
        )
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".write-test-", delete=True) as fh:
                fh.write(b"ok")
        except OSError as exc:
            uid = os.getuid() if hasattr(os, "getuid") else "?"
            rep.problems.append(
                f"data directory {data_dir} is not writable by uid {uid} ({exc.strerror}). "
                f"On a Linux host folder run: sudo chown -R 10001:10001 <folder>, or use a named volume."
            )
    return rep
