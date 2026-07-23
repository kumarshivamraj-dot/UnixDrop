from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib import request

from unixdrop.clipboard_safety import HEALTH_CHECK_CLIPBOARD_PREFIX, HEALTH_CHECK_CLIPBOARD_SOURCE
from unixdrop.config import AppConfig, clipboard_pull_enabled, clipboard_send_enabled, load_config


def _result(name: str, ok: bool, detail: str, **extra: object) -> dict:
    payload = {"name": name, "ok": ok, "detail": detail}
    payload.update(extra)
    return payload


def _result_line(check: dict) -> str:
    return f"[{ 'ok' if check.get('ok') else 'fail' }] {check['name']}: {check['detail']}"


def _request_json(
    cfg: AppConfig,
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    auth: bool = True,
) -> dict:
    data = None
    headers: dict[str, str] = {}
    if auth:
        headers["Authorization"] = f"Bearer {cfg.auth_token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(
        cfg.receiver_url.rstrip("/") + path,
        data=data,
        method=method,
        headers=headers,
    )
    with request.urlopen(req, timeout=cfg.request_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_json_timed(
    cfg: AppConfig,
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    auth: bool = True,
) -> tuple[dict, float]:
    started = time.perf_counter()
    payload_json = _request_json(cfg, path, method=method, payload=payload, auth=auth)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return payload_json, elapsed_ms


def _format_latency(latency_ms: float) -> str:
    if latency_ms < 10:
        return f"{latency_ms:.1f} ms"
    return f"{latency_ms:.0f} ms"


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
    if sys.platform != "darwin":
        return True, "skipped (macOS active tab only)"
    if not shutil.which("osascript"):
        return False, "osascript not found"
    result = subprocess.run(
        ["osascript", "-e", "return 1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, "available" if result.returncode == 0 else (result.stderr.strip() or "error")


def health_checks(config: AppConfig | None = None, config_path: Path | None = None) -> list[dict]:
    try:
        cfg = config or load_config(config_path)
    except Exception as exc:
        return [_result("Config load", False, str(exc))]

    checks: list[dict] = []
    receiver_reachable = False

    try:
        payload, latency_ms = _request_json_timed(cfg, "/health", auth=False)
        receiver_reachable = bool(payload.get("ok"))
        checks.append(
            _result(
                "Peer HTTP receiver reachable",
                receiver_reachable,
                f"reachable ({_format_latency(latency_ms)})",
                latency_ms=latency_ms,
            )
        )
    except Exception as exc:
        checks.append(_result("Peer HTTP receiver reachable", False, str(exc)))

    if receiver_reachable:
        try:
            payload, latency_ms = _request_json_timed(cfg, "/api/ping", method="POST")
            checks.append(
                _result(
                    "send test ping",
                    bool(payload.get("ok")),
                    f"{'pong' if payload.get('pong') else 'unexpected response'} ({_format_latency(latency_ms)})",
                    latency_ms=latency_ms,
                )
            )
        except Exception as exc:
            checks.append(_result("send test ping", False, str(exc)))
    else:
        checks.append(_result("send test ping", False, "skipped (receiver unreachable)", skipped=True))

    send_enabled = clipboard_send_enabled(cfg.clipboard_mode)
    pull_enabled = clipboard_pull_enabled(cfg.clipboard_mode)
    clipboard_enabled = send_enabled or pull_enabled
    if not receiver_reachable:
        checks.append(_result("clipboard API", False, "skipped (receiver unreachable)", skipped=True))
    elif send_enabled:
        probe = f"{HEALTH_CHECK_CLIPBOARD_PREFIX}{uuid.uuid4().hex}"
        expected_hash = hashlib.sha256(probe.encode("utf-8")).hexdigest()
        try:
            payload = _request_json(
                cfg,
                "/api/health/clipboard-check",
                method="POST",
                payload={"text": probe, "source": HEALTH_CHECK_CLIPBOARD_SOURCE},
            )
            checks.append(
                _result(
                    "clipboard push API",
                    bool(payload.get("ok"))
                    and payload.get("stored") is False
                    and payload.get("hash") == expected_hash,
                    "non-mutating check ok"
                    if payload.get("stored") is False and payload.get("hash") == expected_hash
                    else "unexpected clipboard health response",
                )
            )
        except Exception as exc:
            checks.append(_result("clipboard push API", False, str(exc)))

    if receiver_reachable and pull_enabled:
        try:
            _request_json(cfg, "/api/clipboard")
            checks.append(_result("clipboard read", True, "readable"))
        except Exception as exc:
            checks.append(_result("clipboard read", False, str(exc)))
    elif receiver_reachable and not clipboard_enabled:
        checks.append(_result("clipboard API", True, "skipped (clipboard mode off)", skipped=True))

    if receiver_reachable:
        try:
            payload = _request_json(cfg, "/api/health/write-check", method="POST")
            checks.append(
                _result(
                    "peer inbox write permission",
                    bool(payload.get("ok")),
                    "writable" if payload.get("ok") else "not writable",
                )
            )
        except Exception as exc:
            checks.append(_result("peer inbox write permission", False, str(exc)))
    else:
        checks.append(_result("peer inbox write permission", False, "skipped (receiver unreachable)", skipped=True))

    if receiver_reachable:
        try:
            payload = _request_json(cfg, "/api/diagnostics")
            opener_ok = bool(payload.get("link_opener_available", payload.get("xdg_open_available")))
            opener = payload.get("link_opener", "link opener")
            checks.append(
                _result(
                    "peer link opener availability",
                    opener_ok,
                    f"{opener} available" if opener_ok else f"{opener} missing",
                )
            )
        except Exception as exc:
            checks.append(_result("peer link opener availability", False, str(exc)))
    else:
        checks.append(_result("peer link opener availability", False, "skipped (receiver unreachable)", skipped=True))

    browser_ok, browser_detail = _check_browser_script()
    checks.append(_result("local active-tab script", browser_ok, browser_detail))

    drop_exists = Path(cfg.drop_dir).exists()
    checks.append(_result("drop folder exists", drop_exists, str(cfg.drop_dir)))

    launchd_ok, launchd_detail = _check_launchd()
    checks.append(_result("launchd node service status", launchd_ok, launchd_detail))

    systemd_ok, systemd_detail = _check_systemd_local()
    checks.append(_result("systemd --user node service status", systemd_ok, systemd_detail))

    return checks


def health_report(config: AppConfig | None = None, config_path: Path | None = None) -> dict:
    checks = health_checks(config=config, config_path=config_path)
    return {
        "ok": all(bool(check.get("ok")) for check in checks),
        "checks": checks,
    }


def health_lines(config: AppConfig | None = None, config_path: Path | None = None) -> list[str]:
    return ["Deskbridge health", *[_result_line(check) for check in health_checks(config=config, config_path=config_path)]]


def main() -> None:
    for line in health_lines():
        print(line)


if __name__ == "__main__":
    main()
