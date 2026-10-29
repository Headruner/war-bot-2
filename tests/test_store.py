"""Unit tests for WarStore database path handling."""
import os
import sys

import pytest

from app.store import PROJECT_ROOT, WarStore, resolve_db_path


def test_relative_path_resolves_against_project_root():
    assert resolve_db_path("data/wars.db") == PROJECT_ROOT / "data" / "wars.db"


def test_default_path_is_data_dir():
    assert resolve_db_path(None) == PROJECT_ROOT / "data" / "wars.db"


def test_absolute_path_is_kept(tmp_path):
    target = tmp_path / "x.db"
    assert WarStore(target).db_path == target
    assert WarStore(str(target)).db_path == target


@pytest.mark.asyncio
async def test_connect_creates_missing_directory(tmp_path):
    db_file = tmp_path / "nested" / "wars.db"
    store = WarStore(db_file)
    await store.connect()
    try:
        assert db_file.exists()
    finally:
        await store.close()


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="directory permissions are not enforced for root or on Windows",
)
@pytest.mark.asyncio
async def test_connect_rejects_unwritable_directory(tmp_path):
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        with pytest.raises(PermissionError, match="not writable"):
            await WarStore(ro_dir / "wars.db").connect()
    finally:
        ro_dir.chmod(0o755)
