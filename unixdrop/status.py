from __future__ import annotations

import json
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
    return json.loads(STATE_FILE.read_text())


def _fetch_json(path: str) -> dict:
    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {CONFIG.auth_token}"},
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _check_health() -> tuple[bool, str]:
    try:
        req = request.Request(CONFIG.receiver_url.rstrip("/") + "/health")
        with request.urlopen(req, timeout=CONFIG.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok")), "reachable"
    except URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)


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


def _vault_status(state: dict) -> list[str]:
    if not CONFIG.obsidian_enabled:
        return ["vault: disabled"]

    local_entries = {entry.path: entry for entry in build_manifest(CONFIG.obsidian_vault_dir, CONFIG)}
    try:
        remote_manifest = _fetch_json("/api/vault/manifest")
    except Exception as exc:
        return [
            f"vault: local={len(local_entries)} remote=unknown",
            f"vault error: {exc}",
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
        f"vault: local={len(local_entries)} remote={len(remote_entries)} mismatched={len(mismatched)} local_only={len(only_local)} remote_only={len(only_remote)}"
    ]

    if mismatched:
        lines.append("mismatch sample: " + ", ".join(mismatched[:5]))
    if only_local:
        lines.append("local-only sample: " + ", ".join(only_local[:5]))
    if only_remote:
        lines.append("remote-only sample: " + ", ".join(only_remote[:5]))

    last_sync_epoch = None
    if STATE_FILE.exists():
        last_sync_epoch = STATE_FILE.stat().st_mtime
    lines.append(f"last state write: {_format_age(last_sync_epoch)}")

    known = state.get("vault", {})
    lines.append(f"tracked vault files: {len(known)}")
    return lines


def main() -> None:
    state = _read_state()
    ok, detail = _check_health()

    print("UnixDrop status")
    print(f"receiver: {'ok' if ok else 'down'} ({detail})")
    print(f"receiver_url: {CONFIG.receiver_url}")
    print(f"obsidian_enabled: {CONFIG.obsidian_enabled}")

    last_clipboard = state.get("last_clipboard", "")
    if last_clipboard:
        clipped = last_clipboard if len(last_clipboard) <= 100 else last_clipboard[:97] + "..."
        print(f"last clipboard url: {clipped}")
    else:
        print("last clipboard url: none")

    for line in _vault_status(state):
        print(line)


if __name__ == "__main__":
    main()
