from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib import request

from unixdrop.config import clipboard_pull_enabled, clipboard_send_enabled, load_config


CONFIG = load_config()


def _result(name: str, ok: bool, detail: str) -> str:
    return f"[{ 'ok' if ok else 'fail' }] {name}: {detail}"


def _request_json(path: str, method: str = "GET", payload: dict | None = None, auth: bool = True) -> dict:
    data = None
    headers: dict[str, str] = {}
    if auth:
        headers["Authorization"] = f"Bearer {CONFIG.auth_token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + path,
        data=data,
        method=method,
        headers=headers,
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _check_launchd() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.unixdrop.agent"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return True, "not available on this machine"
    return result.returncode == 0, "loaded" if result.returncode == 0 else "not loaded"


def _check_systemd_local() -> tuple[bool, str]:
    if not shutil.which("systemctl"):
        return True, "not available on this machine"
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "unixdrop-receiver.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    active = result.stdout.strip() == "active"
    if active:
        return True, "active"
    message = result.stdout.strip() or result.stderr.strip() or "inactive"
    return False, message


def _check_browser_script() -> tuple[bool, str]:
    if not shutil.which("osascript"):
        return False, "osascript not found"
    result = subprocess.run(
        ["osascript", "-e", "return 1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, "available" if result.returncode == 0 else (result.stderr.strip() or "error")


def health_lines() -> list[str]:
    lines = ["Deskbridge health"]
    receiver_reachable = False

    try:
        payload = _request_json("/health", auth=False)
        receiver_reachable = bool(payload.get("ok"))
        lines.append(_result("HTTP receiver reachable", receiver_reachable, "reachable"))
    except Exception as exc:
        lines.append(_result("HTTP receiver reachable", False, str(exc)))

    if receiver_reachable:
        try:
            payload = _request_json("/api/ping", method="POST")
            lines.append(_result("send test ping", bool(payload.get("ok")), "pong" if payload.get("pong") else "unexpected response"))
        except Exception as exc:
            lines.append(_result("send test ping", False, str(exc)))
    else:
        lines.append(_result("send test ping", False, "skipped (receiver unreachable)"))

    send_enabled = clipboard_send_enabled(CONFIG.clipboard_mode)
    pull_enabled = clipboard_pull_enabled(CONFIG.clipboard_mode)
    clipboard_enabled = send_enabled or pull_enabled
    if not receiver_reachable:
        lines.append(_result("clipboard roundtrip", False, "skipped (receiver unreachable)"))
    elif clipboard_enabled and send_enabled:
        probe = f"deskbridge-health-{uuid.uuid4().hex}"
        try:
            _request_json("/api/clipboard", method="POST", payload={"text": probe, "source": "health-check"})
            pull_ok = True
            if pull_enabled:
                payload = _request_json("/api/clipboard")
                pull_ok = payload.get("text") == probe
            lines.append(_result("clipboard roundtrip", pull_ok, "ok" if pull_ok else "roundtrip mismatch"))
        except Exception as exc:
            lines.append(_result("clipboard roundtrip", False, str(exc)))
    elif pull_enabled:
        try:
            _request_json("/api/clipboard")
            lines.append(_result("clipboard read", True, "readable"))
        except Exception as exc:
            lines.append(_result("clipboard read", False, str(exc)))
    else:
        lines.append(_result("clipboard roundtrip", True, "skipped (clipboard mode off)"))

    if receiver_reachable:
        try:
            payload = _request_json("/api/health/write-check", method="POST")
            lines.append(_result("Linux inbox write permission", bool(payload.get("ok")), "writable" if payload.get("ok") else "not writable"))
        except Exception as exc:
            lines.append(_result("Linux inbox write permission", False, str(exc)))
    else:
        lines.append(_result("Linux inbox write permission", False, "skipped (receiver unreachable)"))

    if receiver_reachable:
        try:
            payload = _request_json("/api/diagnostics")
            xdg_ok = bool(payload.get("xdg_open_available"))
            lines.append(_result("xdg-open availability on Linux", xdg_ok, "available" if xdg_ok else "missing"))
        except Exception as exc:
            lines.append(_result("xdg-open availability on Linux", False, str(exc)))
    else:
        lines.append(_result("xdg-open availability on Linux", False, "skipped (receiver unreachable)"))

    browser_ok, browser_detail = _check_browser_script()
    lines.append(_result("macOS browser tab script", browser_ok, browser_detail))

    drop_exists = Path(CONFIG.drop_dir).exists()
    lines.append(_result("drop folder exists", drop_exists, str(CONFIG.drop_dir)))

    launchd_ok, launchd_detail = _check_launchd()
    lines.append(_result("launchd service status", launchd_ok, launchd_detail))

    systemd_ok, systemd_detail = _check_systemd_local()
    lines.append(_result("systemd --user receiver status (local check)", systemd_ok, systemd_detail))

    return lines


def main() -> None:
    for line in health_lines():
        print(line)


if __name__ == "__main__":
    main()
