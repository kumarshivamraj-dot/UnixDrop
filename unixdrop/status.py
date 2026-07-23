from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request
from urllib.error import URLError

from unixdrop.config import AppConfig, DEFAULT_CONFIG_PATH, ENV_CONFIG_PATH, load_config
from unixdrop.vault import build_manifest


def _state_file(config: AppConfig | None = None) -> Path:
    cfg = config or load_config()
    return cfg.state_dir / "mac_state.json"


def _config_display_path(config_path: Path | None = None) -> Path:
    env_path = os.environ.get(ENV_CONFIG_PATH)
    return (config_path or (Path(env_path) if env_path else DEFAULT_CONFIG_PATH)).expanduser()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_state(state_file: Path | None = None, config: AppConfig | None = None) -> dict:
    path = state_file or _state_file(config)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _fetch_json(cfg: AppConfig, path: str) -> dict:
    req = request.Request(
        cfg.receiver_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {cfg.auth_token}"},
    )
    with request.urlopen(req, timeout=cfg.request_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _check_health(cfg: AppConfig) -> tuple[bool, dict, str, float | None]:
    started = time.perf_counter()
    try:
        req = request.Request(cfg.receiver_url.rstrip("/") + "/health")
        with request.urlopen(req, timeout=cfg.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        latency_ms = (time.perf_counter() - started) * 1000.0
        return bool(payload.get("ok")), payload, "reachable", latency_ms
    except URLError as exc:
        return False, {}, str(exc.reason), None
    except Exception as exc:
        return False, {}, str(exc), None


def _format_latency(latency_ms: float | None) -> str:
    if latency_ms is None:
        return "unknown"
    if latency_ms < 10:
        return f"{latency_ms:.1f} ms"
    return f"{latency_ms:.0f} ms"


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


def _pending_drop_files(cfg: AppConfig) -> int:
    if not cfg.drop_dir.exists():
        return 0
    return sum(1 for path in cfg.drop_dir.iterdir() if path.is_file())


def _vault_status(cfg: AppConfig, state: dict, state_file: Path) -> list[str]:
    if not cfg.obsidian_enabled:
        return ["obsidian sync enabled: false"]

    local_entries = {entry.path: entry for entry in build_manifest(cfg.obsidian_vault_dir, cfg)}
    try:
        remote_manifest = _fetch_json(cfg, "/api/vault/manifest")
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

    last_sync_epoch = state_file.stat().st_mtime if state_file.exists() else None
    lines.append(f"last state write: {_format_age(last_sync_epoch)}")

    known = state.get("vault", {})
    lines.append(f"tracked vault files: {len(known)}")
    return lines


def status_lines(config: AppConfig | None = None, config_path: Path | None = None) -> list[str]:
    try:
        cfg = config or load_config(config_path)
    except Exception as exc:
        return ["Deskbridge status", f"Config load: {exc}"]

    state_file = _state_file(cfg)
    state = _read_state(state_file)
    receiver_ok, health_payload, detail, latency_ms = _check_health(cfg)
    service_ok, service_detail = _check_local_node_service()

    lines = ["Deskbridge status"]
    lines.append(f"config file: {_config_display_path(config_path)}")
    lines.append(f"peer receiver URL: {cfg.receiver_url}")
    lines.append(f"Local node service running: {'yes' if service_ok else 'no'} ({service_detail})")
    lines.append(f"Peer receiver reachable: {'yes' if receiver_ok else 'no'} ({detail})")
    lines.append(f"Peer receiver latency: {_format_latency(latency_ms)}")
    lines.append(f"Peer receiver version: {health_payload.get('version', 'unknown')}")
    lines.append(f"peer hostname: {health_payload.get('hostname', 'unknown')}")
    lines.append(f"auto_open_links: {health_payload.get('auto_open_links', cfg.auto_open_links)}")
    lines.append(f"clipboard_mode: {health_payload.get('clipboard_mode', cfg.clipboard_mode)}")
    lines.append(f"deskflow_enabled: {'yes' if cfg.deskflow_enabled else 'no'}")
    lines.append(f"deskflow_role: {cfg.deskflow_role}")
    lines.append(f"local drop folder: {cfg.drop_dir}")
    lines.append(f"local inbox: {cfg.inbox_dir}")
    lines.append(f"pending files in drop folder: {_pending_drop_files(cfg)}")
    lines.append(f"last upload result: {state.get('last_upload_result', 'none')}")

    lines.extend(_vault_status(cfg, state, state_file))
    lines.append("Mouse/keyboard sharing is managed by Deskflow when deskflow.role is configured.")
    return lines


def status_report(config: AppConfig | None = None, config_path: Path | None = None) -> dict:
    lines = status_lines(config=config, config_path=config_path)
    details: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        details[key.strip()] = value.strip()
    required_ok = ("Local node service running", "Peer receiver reachable")
    ok = "Config load" not in details and all(
        details.get(key, "").lower().startswith("yes") for key in required_ok
    )
    return {"ok": ok, "lines": lines, "details": details}


def main() -> None:
    for line in status_lines():
        print(line)


if __name__ == "__main__":
    main()
