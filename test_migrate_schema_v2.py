import json
from pathlib import Path

import pytest

from migrate_schema_v2 import migrate


def _write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), "utf-8")


def test_dry_run_and_migration_idempotent(tmp_path):
    _write(tmp_path / "characters.json", {"1": {"name": "甲"}})
    _write(tmp_path / "worlds.json", {"旧世界": {"content": "总览"}})
    before = sorted(p.name for p in tmp_path.iterdir())
    report = migrate(tmp_path, dry_run=True)
    assert report["new_entries"] == 1 and sorted(p.name for p in tmp_path.iterdir()) == before
    migrate(tmp_path)
    assert (tmp_path / "characters.json.bak.v1").read_text("utf-8") == json.dumps({"1": {"name": "甲"}}, ensure_ascii=False)
    first = (tmp_path / "worlds.json").read_bytes()
    report2 = migrate(tmp_path)
    assert not report2["changed_files"] and (tmp_path / "worlds.json").read_bytes() == first


def test_broken_input_changes_nothing(tmp_path):
    _write(tmp_path / "characters.json", {"1": {"name": "甲"}})
    (tmp_path / "worlds.json").write_text("{bad", "utf-8")
    before = (tmp_path / "characters.json").read_bytes()
    with pytest.raises(json.JSONDecodeError):
        migrate(tmp_path)
    assert (tmp_path / "characters.json").read_bytes() == before
    assert not (tmp_path / "characters.json.bak.v1").exists()
