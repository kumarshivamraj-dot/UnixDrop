from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from urllib import parse, request

from unixdrop.config import load_config
from unixdrop.vault import build_manifest, file_sha256


CONFIG = load_config()
STATE_FILE = CONFIG.state_dir / "mac_state.json"


def _ensure_dirs() -> None:
    CONFIG.sync_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.state_dir.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_clipboard": "", "files": {}, "vault": {}}
    return json.loads(STATE_FILE.read_text())


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def _clipboard_text() -> str:
    result = subprocess.run(
        ["pbpaste"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _post_json(path: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + path,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {CONFIG.auth_token}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds):
        return


def _post_file(file_path: Path) -> None:
    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + "/api/file",
        data=file_path.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {CONFIG.auth_token}",
            "Content-Type": "application/octet-stream",
            "X-Filename": file_path.name,
        },
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds):
        return


def _file_digest(file_path: Path) -> str:
    return file_sha256(file_path)


def _sync_clipboard_url(state: dict) -> None:
    current = _clipboard_text()
    if not current or current == state.get("last_clipboard"):
        return

    state["last_clipboard"] = current
    if not _looks_like_url(current):
        return

    _post_json("/api/link", {"url": current, "source": "mac-clipboard"})


def _sync_files(state: dict) -> None:
    tracked = state.setdefault("files", {})
    for file_path in sorted(CONFIG.sync_dir.iterdir()):
        if not file_path.is_file():
            continue

        digest = _file_digest(file_path)
        previous = tracked.get(file_path.name)
        if previous == digest:
            continue

        _post_file(file_path)
        tracked[file_path.name] = digest


def _fetch_json(path: str) -> dict:
    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {CONFIG.auth_token}"},
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_bytes(path: str) -> bytes:
    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {CONFIG.auth_token}"},
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds) as response:
        return response.read()


def _post_vault_file(relative_path: str, file_path: Path) -> None:
    stat = file_path.stat()
    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + "/api/vault/file",
        data=file_path.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {CONFIG.auth_token}",
            "Content-Type": "application/octet-stream",
            "X-Relative-Path": relative_path,
            "X-File-Mtime": str(stat.st_mtime),
        },
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds):
        return


def _write_conflict_copy(file_path: Path, incoming_bytes: bytes, remote_sha: str) -> None:
    conflict_name = f"{file_path.stem}.linux-conflict-{remote_sha[:8]}{file_path.suffix}"
    conflict_path = file_path.with_name(conflict_name)
    conflict_path.write_bytes(incoming_bytes)


def _sync_obsidian_vault(state: dict) -> None:
    if not CONFIG.obsidian_enabled:
        return

    CONFIG.obsidian_vault_dir.mkdir(parents=True, exist_ok=True)
    local_entries = {
        entry.path: entry
        for entry in build_manifest(CONFIG.obsidian_vault_dir, CONFIG)
    }
    remote_manifest = _fetch_json("/api/vault/manifest")
    remote_entries = {entry["path"]: entry for entry in remote_manifest.get("files", [])}
    known = state.setdefault("vault", {})

    all_paths = sorted(set(local_entries) | set(remote_entries))
    for relative_path in all_paths:
        local = local_entries.get(relative_path)
        remote = remote_entries.get(relative_path)

        if local and not remote:
            _post_vault_file(relative_path, CONFIG.obsidian_vault_dir / relative_path)
            known[relative_path] = local.sha256
            continue

        if remote and not local:
            data = _fetch_bytes("/api/vault/file?path=" + parse.quote(relative_path))
            destination = CONFIG.obsidian_vault_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            os.utime(destination, (remote["mtime"], remote["mtime"]))
            known[relative_path] = remote["sha256"]
            continue

        assert local is not None and remote is not None
        if local.sha256 == remote["sha256"]:
            known[relative_path] = local.sha256
            continue

        last_synced = known.get(relative_path)
        if last_synced == remote["sha256"]:
            _post_vault_file(relative_path, CONFIG.obsidian_vault_dir / relative_path)
            known[relative_path] = local.sha256
            continue

        if last_synced == local.sha256:
            data = _fetch_bytes("/api/vault/file?path=" + parse.quote(relative_path))
            destination = CONFIG.obsidian_vault_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            os.utime(destination, (remote["mtime"], remote["mtime"]))
            known[relative_path] = remote["sha256"]
            continue

        data = _fetch_bytes("/api/vault/file?path=" + parse.quote(relative_path))
        local_path = CONFIG.obsidian_vault_dir / relative_path
        _write_conflict_copy(local_path, data, remote["sha256"])
        _post_vault_file(relative_path, local_path)
        known[relative_path] = file_sha256(local_path)


def main() -> None:
    _ensure_dirs()
    state = _load_state()
    last_file_scan = 0.0
    last_vault_scan = 0.0

    while True:
        try:
            _sync_clipboard_url(state)

            now = time.time()
            if now - last_file_scan >= CONFIG.file_poll_seconds:
                _sync_files(state)
                last_file_scan = now

            if CONFIG.obsidian_enabled and now - last_vault_scan >= CONFIG.obsidian_poll_seconds:
                _sync_obsidian_vault(state)
                last_vault_scan = now

            _save_state(state)
        except Exception as exc:
            print(f"UnixDrop mac agent error: {exc}")

        time.sleep(CONFIG.clipboard_poll_seconds)


if __name__ == "__main__":
    main()
