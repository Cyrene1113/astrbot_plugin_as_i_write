"""如我所书 schema v2 独立迁移工具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from .lore import CURRENT_SCHEMA_VERSION, normalize_collections
except ImportError:
    from lore import CURRENT_SCHEMA_VERSION, normalize_collections

_DEFAULT_DATA_DIR = Path(os.environ.get("ASTRBOT_DATA_DIR", "data")) / "plugin_data" / "astrbot_plugin_as_i_write"


def _read_json(path: Path) -> tuple[bytes, dict[str, Any]]:
    raw = path.read_bytes() if path.exists() else b"{}"
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 顶层必须是对象")
    return raw, value


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _validate_collection(characters: dict, worlds: dict) -> None:
    for uid, character in characters.items():
        if character.get("schema_version") != CURRENT_SCHEMA_VERSION:
            raise ValueError(f"角色 {uid} schema_version 校验失败")
    for name, world in worlds.items():
        if world.get("schema_version") != CURRENT_SCHEMA_VERSION:
            raise ValueError(f"世界 {name} schema_version 校验失败")
        ids = [entry.get("id") for entry in world.get("entries", [])]
        if not all(ids) or len(ids) != len(set(ids)):
            raise ValueError(f"世界 {name} 条目 ID 缺失或重复")


def migrate(data_dir: Path, *, dry_run: bool = False, force_backup: bool = False) -> dict[str, Any]:
    data_dir = Path(data_dir)
    char_path, world_path = data_dir / "characters.json", data_dir / "worlds.json"
    char_raw, characters = _read_json(char_path)
    world_raw, worlds = _read_json(world_path)
    normalized_c, normalized_w, changed_c, changed_w = normalize_collections(characters, worlds)
    _validate_collection(normalized_c, normalized_w)
    old_entries = sum(len(w.get("entries", [])) for w in worlds.values() if isinstance(w, dict))
    new_entries = sum(len(w.get("entries", [])) for w in normalized_w.values())
    report = {
        "characters": len(normalized_c), "worlds": len(normalized_w),
        "new_entries": max(0, new_entries - old_entries),
        "changed_files": [name for name, changed in (("characters.json", changed_c), ("worlds.json", changed_w)) if changed],
        "dry_run": dry_run,
    }
    if dry_run or not report["changed_files"]:
        return report
    backups = {char_path: char_path.with_name("characters.json.bak.v1"), world_path: world_path.with_name("worlds.json.bak.v1")}
    existing = [backup for backup in backups.values() if backup.exists()]
    if existing and not force_backup:
        raise FileExistsError("备份已存在；请确认后使用 --force-backup（仍不会覆盖已有备份）: " + ", ".join(str(p) for p in existing))
    for source, backup in backups.items():
        if not backup.exists():
            backup.write_bytes(char_raw if source == char_path else world_raw)
    try:
        _atomic_write(char_path, normalized_c)
        _atomic_write(world_path, normalized_w)
        _, check_c = _read_json(char_path)
        _, check_w = _read_json(world_path)
        _validate_collection(check_c, check_w)
    except Exception:
        for target, backup in backups.items():
            if backup.exists():
                shutil.copyfile(backup, target)
        raise
    report["backup_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backups.values()}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移如我所书数据到 schema v2")
    parser.add_argument("--data-dir", type=Path, default=_DEFAULT_DATA_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-backup", action="store_true", help="允许已有备份时继续；不会覆盖唯一备份")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(migrate(args.data_dir, dry_run=args.dry_run, force_backup=args.force_backup), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"迁移失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
