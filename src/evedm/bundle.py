"""JSON bundle import/export, schema evedm-characters v1 (IMP-01..IMP-09, EXP-01..EXP-02)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from .characters import LIMITED, CharacterService
from .sso import SsoError

log = logging.getLogger("evedm.bundle")

MAX_BYTES = 1_000_000


class BundleRejected(Exception):
    """The whole file is rejected; nothing was stored (IMP-02, IMP-03)."""

    def __init__(self, reason: str, errors: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.errors = errors or []


@dataclass
class EntryResult:
    index: int
    result: str  # Added | Updated | Limited | Rejected
    character_id: int | None = None
    name: str | None = None
    reason: str = ""
    missing_scopes: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    text = resources.files("evedm").joinpath("schema/evedm-characters.v1.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def validate(doc: Any) -> list[str]:
    validator = Draft202012Validator(schema(), format_checker=FormatChecker())
    errors = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        errors.append(f"{path}: {err.message}")
    return errors


def parse(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        raise BundleRejected("file is larger than 1 MB")
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleRejected(f"not valid UTF-8 JSON: {exc}") from exc
    errors = validate(doc)
    if errors:
        raise BundleRejected("file does not match schema evedm-characters v1", errors)
    return doc


async def import_bundle(raw: bytes, chars: CharacterService, owner_id: int = 0) -> list[EntryResult]:
    doc = parse(raw)
    if doc["client_id"] != chars.settings.eve.client_id:
        raise BundleRejected("tokens were issued by another EVE application")
    if chars.auth is None:
        raise BundleRejected("EVE SSO is not configured on this instance")
    results: list[EntryResult] = []
    for i, entry in enumerate(doc["characters"]):
        res = EntryResult(
            index=i, result="Rejected", character_id=entry.get("character_id"), name=entry.get("character_name")
        )
        try:
            hinted = None
            if entry.get("access_token"):
                hinted = await chars.auth.verify(entry["access_token"], allow_expired=True)  # IMP-04
                if res.character_id and res.character_id != hinted.character_id:
                    raise SsoError("character_mismatch", "character_id differs from the access token")
            ts = await chars.auth.refresh(entry["refresh_token"])  # IMP-05
            ident = ts.identity
            for expected in (hinted.character_id if hinted else None, res.character_id):
                if expected and expected != ident.character_id:
                    raise SsoError("character_mismatch", "refresh token belongs to another character")
            existed = chars.db.get_character(ident.character_id, owner_id) is not None
            _, status = chars.store(ts, owner_id=owner_id)  # IMP-06 stores the refreshed (newest) refresh token
            if not existed:
                if entry.get("tags"):
                    chars.db.set_tags(ident.character_id, entry["tags"], owner_id)
                if entry.get("enabled") is False:
                    chars.db.set_enabled(ident.character_id, False, owner_id)
            res.character_id, res.name = ident.character_id, ident.name
            res.missing_scopes = chars.missing_scopes(ident.scopes)
            res.result = "Limited" if status == LIMITED else ("Updated" if existed else "Added")
        except SsoError as exc:
            res.reason = exc.code
        results.append(res)
    log.info(
        "bundle import: %s",
        {r: sum(1 for x in results if x.result == r) for r in ("Added", "Updated", "Limited", "Rejected")},
    )
    return results


def report(results: list[EntryResult]) -> dict[str, Any]:
    return {
        "summary": {r: sum(1 for x in results if x.result == r) for r in ("Added", "Updated", "Limited", "Rejected")},
        "entries": [asdict(r) for r in results],
    }


def export_bundle(chars: CharacterService, character_ids: list[int] | None = None, owner_id: int = 0) -> dict[str, Any]:
    """EXP-01: refresh token always; access token only while still valid. Demo characters are skipped."""
    now = time.time()
    entries = []
    for c in chars.db.list_characters(owner_id):
        if c.demo or (character_ids and c.character_id not in character_ids):
            continue
        tok = chars.db.get_tokens(c.character_id, owner_id)
        if tok is None:
            continue
        item: dict[str, Any] = {
            "character_id": c.character_id,
            "character_name": c.name,
            "refresh_token": chars.cipher.decrypt(tok["refresh_token"]),
            "scopes": c.scopes,
            "tags": c.tags,
            "enabled": c.enabled,
        }
        if tok["access_token"] and tok["access_expires_at"] > now + 30:
            item["access_token"] = chars.cipher.decrypt(tok["access_token"])
            item["access_token_expires_at"] = datetime.fromtimestamp(tok["access_expires_at"], UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        entries.append(item)
    if not entries:
        raise BundleRejected("no exportable characters")
    return {
        "schema_version": 1,
        "client_id": chars.settings.eve.client_id,
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exported_from": chars.settings.instance_label,
        "characters": entries,
    }
