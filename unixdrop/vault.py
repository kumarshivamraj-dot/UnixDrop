from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_bytes_atomic(destination: Path, data: bytes, mtime: float | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mtime is not None and mtime > 0:
            os.utime(temp_path, (mtime, mtime))
        os.replace(temp_path, destination)
        temp_path = None
        _fsync_directory(destination.parent)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def normalize_vault_relative_path(relative_path: str) -> str:
    raw_path = str(relative_path).strip()
    if not raw_path:
        raise ValueError("vault path must not be empty")
    if "\x00" in raw_path:
        raise ValueError("vault path must not contain NUL bytes")

    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute():
        raise ValueError("vault path must be relative")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("vault path must stay inside the vault")

    normalized = str(candidate)
    if not normalized or normalized == ".":
        raise ValueError("vault path must not be empty")
    return normalized


def vault_path(vault_dir: Path, relative_path: str) -> Path:
    normalized = normalize_vault_relative_path(relative_path)
    root = vault_dir.resolve(strict=False)
    destination = (root / normalized).resolve(strict=False)
    try:
        destination.relative_to(root)
    except ValueError:
        raise ValueError("vault path must stay inside the vault") from None
    return destination


def _normalize_patterns(config: AppConfig) -> list[str]:
    patterns = config.obsidian_excludes or []
    return list(patterns)


def should_skip_relative(relative_path: str, config: AppConfig) -> bool:
    try:
        normalized = normalize_vault_relative_path(relative_path)
    except ValueError:
        return True

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
