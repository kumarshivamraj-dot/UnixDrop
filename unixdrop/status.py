from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request
from urllib.error import URLError

from unixdrop.config import load_config
from unixdrop.vault import build_manifest


CONFIG = load_config()
STATE_FILE = CONFIG.state_dir / "mac_state.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _fetch_json(path: str) -> dict:
    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {CONFIG.auth_token}"},
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _check_health() -> tuple[bool, dict, str]:
    try:
        req = request.Request(CONFIG.receiver_url.rstrip("/") + "/health")
        with request.urlopen(req, timeout=CONFIG.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok")), payload, "reachable"
    except URLError as exc:
        return False, {}, str(exc.reason)
    except Exception as exc:
        return False, {}, str(exc)


def _format_age(timestamp: float | None) -> str:
    if not timestamp:
        return "unknown"
    seconds = int((_utc_now().timestamp()) - timestamp)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _check_local_node_service() -> tuple[bool, str]:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["launchctl", "list", "com.unixdrop.agent"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return False, "launchctl not found"
        return result.returncode == 0, "launchd com.unixdrop.agent"

    if sys.platform.startswith("linux"):
        if not shutil.which("systemctl"):
            return False, "systemctl not found"
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "unixdrop-receiver.service"],
            capture_output=True,
            text=True,
            check=False,
        )
        active = result.stdout.strip() == "active"
        detail = result.stdout.strip() or result.stderr.strip() or "inactive"
        return active, f"systemd unixdrop-receiver.service: {detail}"

    return False, f"unsupported platform: {sys.platform}"


def _pending_drop_files() -> int:
    if not CONFIG.drop_dir.exists():
        return 0
    return sum(1 for path in CONFIG.drop_dir.iterdir() if path.is_file())


def _vault_status(state: dict) -> list[str]:
    if not CONFIG.obsidian_enabled:
        return ["obsidian sync enabled: false"]

    local_entries = {entry.path: entry for entry in build_manifest(CONFIG.obsidian_vault_dir, CONFIG)}
    try:
        remote_manifest = _fetch_json("/api/vault/manifest")
    except Exception as exc:
        return [
            "obsidian sync enabled: true",
            f"vault drift: unknown ({exc})",
        ]

    remote_entries = {entry["path"]: entry for entry in remote_manifest.get("files", [])}
    only_local = sorted(set(local_entries) - set(remote_entries))
    only_remote = sorted(set(remote_entries) - set(local_entries))
    mismatched = sorted(
        path
        for path in set(local_entries) & set(remote_entries)
        if local_entries[path].sha256 != remote_entries[path]["sha256"]
    )

    lines = [
        "obsidian sync enabled: true",
        (
            "vault drift: "
            f"mismatched={len(mismatched)} local_only={len(only_local)} remote_only={len(only_remote)}"
        ),
    ]

    last_sync_epoch = STATE_FILE.stat().st_mtime if STATE_FILE.exists() else None
    lines.append(f"last state write: {_format_age(last_sync_epoch)}")

    known = state.get("vault", {})
    lines.append(f"tracked vault files: {len(known)}")
    return lines


def status_lines() -> list[str]:
    state = _read_state()
    receiver_ok, health_payload, detail = _check_health()
    service_ok, service_detail = _check_local_node_service()

    lines = ["Deskbridge status"]
    lines.append(f"Local node service running: {'yes' if service_ok else 'no'} ({service_detail})")
    lines.append(f"Peer receiver reachable: {'yes' if receiver_ok else 'no'} ({detail})")
    lines.append(f"Peer receiver version: {health_payload.get('version', 'unknown')}")
    lines.append(f"auto_open_links: {health_payload.get('auto_open_links', CONFIG.auto_open_links)}")
    lines.append(f"clipboard_mode: {health_payload.get('clipboard_mode', CONFIG.clipboard_mode)}")
    lines.append(f"local drop folder: {CONFIG.drop_dir}")
    lines.append(f"local inbox: {CONFIG.inbox_dir}")
    lines.append(f"pending files in drop folder: {_pending_drop_files()}")
    lines.append(f"last upload result: {state.get('last_upload_result', 'none')}")

    lines.extend(_vault_status(state))
    lines.append("Mouse/keyboard sharing is managed by Deskflow when deskflow.role is configured.")
    return lines


def main() -> None:
    for line in status_lines():
        print(line)


if __name__ == "__main__":
    main()
