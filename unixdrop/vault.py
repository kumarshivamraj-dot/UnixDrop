from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from unixdrop.config import AppConfig


@dataclass
class VaultEntry:
    path: str
    sha256: str
    size: int
    mtime: float


def file_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _normalize_patterns(config: AppConfig) -> list[str]:
    patterns = config.obsidian_excludes or []
    return list(patterns)


def should_skip_relative(relative_path: str, config: AppConfig) -> bool:
    normalized = relative_path.strip("/")
    if not normalized:
        return False

    for pattern in _normalize_patterns(config):
        cleaned = pattern.strip("/")
        if fnmatch.fnmatch(normalized, cleaned):
            return True
        if normalized == cleaned or normalized.startswith(cleaned + "/"):
            return True
    return False


def build_manifest(vault_dir: Path, config: AppConfig) -> list[VaultEntry]:
    entries: list[VaultEntry] = []
    if not vault_dir.exists():
        return entries

    for file_path in sorted(vault_dir.rglob("*")):
        if not file_path.is_file():
            continue
        relative_path = file_path.relative_to(vault_dir).as_posix()
        if should_skip_relative(relative_path, config):
            continue
        stat = file_path.stat()
        entries.append(
            VaultEntry(
                path=relative_path,
                sha256=file_sha256(file_path),
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
        )
    return entries


def manifest_to_json(entries: list[VaultEntry]) -> bytes:
    payload = {
        "files": [
            {
                "path": entry.path,
                "sha256": entry.sha256,
                "size": entry.size,
                "mtime": entry.mtime,
            }
            for entry in entries
        ]
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")
