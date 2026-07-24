from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import parse, request

from unixdrop.clipboard_safety import is_health_check_clipboard_text
from unixdrop.config import (
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG_PATH,
    clipboard_pull_enabled,
    clipboard_send_enabled,
    deskflow_start_script,
    load_config,
)
from unixdrop.http_transfer import post_file
from unixdrop.vault import (
    build_manifest,
    file_sha256,
    normalize_vault_relative_path,
    should_skip_relative,
    vault_path,
    write_bytes_atomic,
)


CONFIG = load_config()
STATE_FILE = CONFIG.state_dir / "mac_state.json"
CONFIG_MTIME_NS: int | None = None
DESKFLOW_RETRY_SECONDS = 30
_DESKFLOW_RETRY_AFTER = 0.0
_DESKFLOW_SUPERVISION_DISABLED = False
PEER_FAILURE_RETRY_SECONDS = 10
_PEER_RETRY_AFTER = 0.0
_PEER_LAST_ERROR = ""


def _active_config_path() -> Path:
    env_path = os.environ.get(ENV_CONFIG_PATH)
    return (Path(env_path) if env_path else DEFAULT_CONFIG_PATH).expanduser()


def _config_mtime_ns() -> int | None:
    try:
        return _active_config_path().stat().st_mtime_ns
    except OSError:
        return None


CONFIG_MTIME_NS = _config_mtime_ns()


def _default_state() -> dict:
    return {
        "files": {},
        "drop_pending": {},
        "vault": {},
        "last_upload_result": "none",
        "last_local_clipboard_hash": "",
        "last_remote_clipboard_hash": "",
        "last_remote_applied_hash": "",
    }


def _ensure_dirs() -> None:
    CONFIG.drop_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.state_dir.mkdir(parents=True, exist_ok=True)


def _reload_config_if_changed() -> bool:
    global CONFIG, STATE_FILE, CONFIG_MTIME_NS
    current_mtime = _config_mtime_ns()
    if current_mtime is None or current_mtime == CONFIG_MTIME_NS:
        return False

    try:
        next_config = load_config()
    except Exception as exc:
        CONFIG_MTIME_NS = current_mtime
        print(f"UnixDrop config reload failed: {exc}")
        return False

    CONFIG = next_config
    STATE_FILE = CONFIG.state_dir / "mac_state.json"
    CONFIG_MTIME_NS = current_mtime
    _ensure_dirs()
    _record_peer_request_success()
    print(f"UnixDrop config reloaded from {_active_config_path()}")
    return True


def _start_deskflow_process() -> subprocess.Popen[str] | None:
    script = deskflow_start_script(CONFIG, sys.platform)
    if script is None:
        return None
    if not script.exists():
        print(f"Deskflow integration enabled but script not found: {script}")
        return None
    if not os.access(script, os.X_OK):
        print(f"Deskflow integration enabled but script not executable: {script}")
        return None
    try:
        proc = subprocess.Popen([str(script)])
        print(f"Deskflow start requested via UnixDrop mac agent: {script} (pid={proc.pid})")
        return proc
    except Exception as exc:
        print(f"Failed to start Deskflow from mac agent: {exc}")
        return None


def _ensure_deskflow_running(process: subprocess.Popen[str] | None) -> subprocess.Popen[str] | None:
    global _DESKFLOW_RETRY_AFTER, _DESKFLOW_SUPERVISION_DISABLED
    if deskflow_start_script(CONFIG, sys.platform) is None:
        return None
    if _DESKFLOW_SUPERVISION_DISABLED:
        return None
    if process is None:
        if time.monotonic() < _DESKFLOW_RETRY_AFTER:
            return None
        proc = _start_deskflow_process()
        if proc is None:
            _DESKFLOW_RETRY_AFTER = time.monotonic() + DESKFLOW_RETRY_SECONDS
        return proc
    return_code = process.poll()
    if return_code is None:
        return process
    if return_code == 0:
        _DESKFLOW_SUPERVISION_DISABLED = True
        print("Deskflow launcher exited cleanly; assuming Deskflow is already managed externally")
        return None
    _DESKFLOW_RETRY_AFTER = time.monotonic() + DESKFLOW_RETRY_SECONDS
    print(
        f"Deskflow process exited with code {return_code}; "
        f"retrying in {DESKFLOW_RETRY_SECONDS}s"
    )
    return None


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return _default_state()
    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state()
    return loaded if isinstance(loaded, dict) else _default_state()


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_FILE.with_name(f".{STATE_FILE.name}.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, STATE_FILE)


def _clipboard_text() -> str:
    command = _clipboard_get_command()
    if not command:
        return ""
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _set_clipboard_text(text: str) -> None:
    command = _clipboard_set_command()
    if not command:
        return
    subprocess.run(
        command,
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )


def _clipboard_get_command() -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("pbpaste"):
        return ["pbpaste"]
    if shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"]
    return None


def _clipboard_set_command() -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        return ["pbcopy"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _post_json(path: str, payload: dict) -> dict:
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
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _peer_request_allowed() -> bool:
    return time.monotonic() >= _PEER_RETRY_AFTER


def _record_peer_request_success() -> None:
    global _PEER_RETRY_AFTER, _PEER_LAST_ERROR
    _PEER_RETRY_AFTER = 0.0
    _PEER_LAST_ERROR = ""


def _record_peer_request_failure(exc: Exception) -> None:
    global _PEER_RETRY_AFTER, _PEER_LAST_ERROR
    _PEER_RETRY_AFTER = time.monotonic() + PEER_FAILURE_RETRY_SECONDS
    message = str(exc)
    if message != _PEER_LAST_ERROR:
        print(
            "UnixDrop peer unavailable: "
            f"{message}; retrying in {PEER_FAILURE_RETRY_SECONDS}s"
        )
        _PEER_LAST_ERROR = message


def _post_file(file_path: Path) -> dict:
    return post_file(
        url=CONFIG.receiver_url.rstrip("/") + "/api/file",
        file_path=file_path,
        timeout_seconds=CONFIG.request_timeout_seconds,
        headers={
            "Authorization": f"Bearer {CONFIG.auth_token}",
            "Content-Type": "application/octet-stream",
            "X-Filename": file_path.name,
        },
    )


def _sync_clipboard_push(state: dict) -> None:
    if not clipboard_send_enabled(CONFIG.clipboard_mode):
        return

    current = _clipboard_text()
    if not current or len(current) > CONFIG.max_clipboard_chars:
        return

    digest = _hash_text(current)
    if is_health_check_clipboard_text(current):
        state["last_local_clipboard_hash"] = digest
        return
    if digest == state.get("last_local_clipboard_hash"):
        return

    state["last_local_clipboard_hash"] = digest
    if digest == state.get("last_remote_applied_hash"):
        return

    _post_json("/api/clipboard", {"text": current, "source": "local"})
    print("Clipboard local -> peer")


def _pull_remote_clipboard(state: dict) -> None:
    if not clipboard_pull_enabled(CONFIG.clipboard_mode):
        return

    payload = _fetch_json("/api/clipboard")
    remote_text = payload.get("text", "")
    if not isinstance(remote_text, str) or not remote_text:
        return
    if len(remote_text) > CONFIG.max_clipboard_chars:
        return

    remote_hash = payload.get("hash")
    if not isinstance(remote_hash, str) or not remote_hash:
        remote_hash = _hash_text(remote_text)

    if remote_hash == state.get("last_remote_clipboard_hash"):
        return
    state["last_remote_clipboard_hash"] = remote_hash

    if is_health_check_clipboard_text(remote_text):
        return

    if remote_hash == state.get("last_local_clipboard_hash"):
        return

    _set_clipboard_text(remote_text)
    state["last_local_clipboard_hash"] = remote_hash
    state["last_remote_applied_hash"] = remote_hash
    print("Clipboard peer -> local")


def _sync_drop_files(state: dict) -> None:
    tracked = state.setdefault("files", {})
    pending = state.setdefault("drop_pending", {})
    max_bytes = CONFIG.max_file_mb * 1024 * 1024

    now = time.time()
    existing_names = set()
    for file_path in sorted(CONFIG.drop_dir.iterdir()):
        if not file_path.is_file():
            continue

        existing_names.add(file_path.name)
        stat = file_path.stat()
        size = stat.st_size
        mtime = stat.st_mtime

        if size > max_bytes:
            state["last_upload_result"] = f"skipped {file_path.name}: exceeds max_file_mb={CONFIG.max_file_mb}"
            pending.pop(file_path.name, None)
            continue

        previous = pending.get(file_path.name)
        if not previous or previous.get("size") != size or previous.get("mtime") != mtime:
            pending[file_path.name] = {"size": size, "mtime": mtime, "stable_checks": 0}
            continue

        previous["stable_checks"] = int(previous.get("stable_checks", 0)) + 1
        if previous["stable_checks"] < 1:
            continue

        if now - mtime < 1:
            continue

        digest = file_sha256(file_path)
        if tracked.get(file_path.name) == digest:
            continue

        response = _post_file(file_path)
        tracked[file_path.name] = digest
        pending.pop(file_path.name, None)

        remote_path = response.get("path", "")
        state["last_upload_result"] = (
            f"uploaded {file_path.name} ({size} bytes) at {datetime.now().isoformat(timespec='seconds')}"
        )
        print(f"Uploaded to peer inbox: {file_path.name} -> {remote_path}")

        if CONFIG.delete_after_send:
            file_path.unlink(missing_ok=True)
            state["last_upload_result"] += " (deleted local file)"

    stale_names = [name for name in pending if name not in existing_names]
    for name in stale_names:
        pending.pop(name, None)
    stale_tracked = [name for name in tracked if name not in existing_names]
    for name in stale_tracked:
        tracked.pop(name, None)


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
    post_file(
        url=CONFIG.receiver_url.rstrip("/") + "/api/vault/file",
        file_path=file_path,
        timeout_seconds=CONFIG.request_timeout_seconds,
        headers={
            "Authorization": f"Bearer {CONFIG.auth_token}",
            "Content-Type": "application/octet-stream",
            "X-Relative-Path": relative_path,
            "X-File-Mtime": str(stat.st_mtime),
        },
    )


def _write_conflict_copy(file_path: Path, incoming_bytes: bytes, remote_sha: str) -> None:
    conflict_name = f"{file_path.stem}.linux-conflict-{remote_sha[:8]}{file_path.suffix}"
    conflict_path = file_path.with_name(conflict_name)
    write_bytes_atomic(conflict_path, incoming_bytes)


def _sync_obsidian_vault(state: dict) -> None:
    if not CONFIG.obsidian_enabled:
        return

    CONFIG.obsidian_vault_dir.mkdir(parents=True, exist_ok=True)
    local_entries = {
        entry.path: entry
        for entry in build_manifest(CONFIG.obsidian_vault_dir, CONFIG)
    }
    remote_manifest = _fetch_json("/api/vault/manifest")
    remote_entries = {}
    for entry in remote_manifest.get("files", []):
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        try:
            relative_path = normalize_vault_relative_path(str(entry["path"]))
        except ValueError:
            continue
        if should_skip_relative(relative_path, CONFIG):
            continue
        remote_entries[relative_path] = entry
    known = state.setdefault("vault", {})

    all_paths = sorted(set(local_entries) | set(remote_entries))
    for relative_path in all_paths:
        if should_skip_relative(relative_path, CONFIG):
            continue
        local = local_entries.get(relative_path)
        remote = remote_entries.get(relative_path)

        if local and not remote:
            _post_vault_file(relative_path, vault_path(CONFIG.obsidian_vault_dir, relative_path))
            known[relative_path] = local.sha256
            continue

        if remote and not local:
            data = _fetch_bytes("/api/vault/file?path=" + parse.quote(relative_path))
            destination = vault_path(CONFIG.obsidian_vault_dir, relative_path)
            write_bytes_atomic(destination, data, remote["mtime"])
            known[relative_path] = remote["sha256"]
            continue

        assert local is not None and remote is not None
        if local.sha256 == remote["sha256"]:
            known[relative_path] = local.sha256
            continue

        last_synced = known.get(relative_path)
        if last_synced == remote["sha256"]:
            _post_vault_file(relative_path, vault_path(CONFIG.obsidian_vault_dir, relative_path))
            known[relative_path] = local.sha256
            continue

        if last_synced == local.sha256:
            data = _fetch_bytes("/api/vault/file?path=" + parse.quote(relative_path))
            destination = vault_path(CONFIG.obsidian_vault_dir, relative_path)
            write_bytes_atomic(destination, data, remote["mtime"])
            known[relative_path] = remote["sha256"]
            continue

        data = _fetch_bytes("/api/vault/file?path=" + parse.quote(relative_path))
        local_path = vault_path(CONFIG.obsidian_vault_dir, relative_path)
        _write_conflict_copy(local_path, data, remote["sha256"])
        _post_vault_file(relative_path, local_path)
        known[relative_path] = file_sha256(local_path)


def main(*, start_deskflow: bool = True) -> None:
    _ensure_dirs()
    state = _load_state()
    deskflow_process = _start_deskflow_process() if start_deskflow else None
    last_file_scan = 0.0
    last_vault_scan = 0.0

    while True:
        try:
            if _reload_config_if_changed():
                state = _load_state()
            if start_deskflow:
                deskflow_process = _ensure_deskflow_running(deskflow_process)

            if _peer_request_allowed():
                try:
                    _sync_clipboard_push(state)
                    _pull_remote_clipboard(state)
                    _record_peer_request_success()
                except Exception as exc:
                    _record_peer_request_failure(exc)

            now = time.time()
            if now - last_file_scan >= CONFIG.file_poll_seconds:
                if _peer_request_allowed():
                    try:
                        _sync_drop_files(state)
                        _record_peer_request_success()
                    except Exception as exc:
                        state["last_upload_result"] = f"upload failed: {exc}"
                        _record_peer_request_failure(exc)
                last_file_scan = now

            if CONFIG.obsidian_enabled and now - last_vault_scan >= CONFIG.obsidian_poll_seconds:
                if _peer_request_allowed():
                    try:
                        _sync_obsidian_vault(state)
                        _record_peer_request_success()
                    except Exception as exc:
                        _record_peer_request_failure(exc)
                last_vault_scan = now

            _save_state(state)
        except Exception as exc:
            print(f"UnixDrop mac agent error: {exc}")

        time.sleep(CONFIG.clipboard_poll_seconds)


if __name__ == "__main__":
    main()
