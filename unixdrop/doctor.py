from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib import request
from urllib.parse import urljoin

from unixdrop.config import DEFAULT_CONFIG_PATH, ENV_CONFIG_PATH, AppConfig, load_config
from unixdrop.platform_tools import (
    clipboard_tools_status,
    deskflow_binary_status,
    link_opener_status,
    service_manager_status,
)


DEFAULT_FIREFOX_DEBUG_URL = "http://127.0.0.1:9222"
FIREFOX_BROWSER_NAMES = {
    "firefox",
    "firefox-developer",
    "firefox-developer-edition",
    "firefox developer edition",
    "librewolf",
}


@dataclass(frozen=True)
class DoctorCheck:
    status: str
    name: str
    detail: str

    def line(self) -> str:
        return f"[{self.status}] {self.name}: {self.detail}"


def _config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path.expanduser()
    env_path = os.environ.get(ENV_CONFIG_PATH)
    return (Path(env_path) if env_path else DEFAULT_CONFIG_PATH).expanduser()


def _check_config(path: Path) -> tuple[list[DoctorCheck], AppConfig | None]:
    if not path.exists():
        return [DoctorCheck("fail", "Config file", f"missing: {path}")], None

    checks = [DoctorCheck("ok", "Config file", str(path))]
    try:
        cfg = load_config(path)
    except Exception as exc:
        checks.append(DoctorCheck("fail", "Config load", str(exc)))
        return checks, None
    checks.append(DoctorCheck("ok", "Receiver URL", cfg.receiver_url))
    return checks, cfg


def _status_from_requirement(ok: bool, detail: str, *, required: bool) -> DoctorCheck:
    if ok:
        return DoctorCheck("ok", "", detail)
    return DoctorCheck("fail" if required else "warn", "", detail)


def _firefox_list_url(debug_url: str) -> str:
    base = (debug_url or DEFAULT_FIREFOX_DEBUG_URL).strip() or DEFAULT_FIREFOX_DEBUG_URL
    if base.endswith("/json/list"):
        return base
    return urljoin(base.rstrip("/") + "/", "json/list")


def _firefox_debug_configured(cfg: AppConfig) -> bool:
    browser = str(cfg.tabs_default_browser or "auto").strip().lower()
    debug_url = str(cfg.tabs_firefox_debug_url or "").strip()
    return browser in FIREFOX_BROWSER_NAMES or bool(debug_url) and debug_url != DEFAULT_FIREFOX_DEBUG_URL


def _check_firefox_debug(cfg: AppConfig | None) -> DoctorCheck:
    if cfg is None:
        return DoctorCheck("warn", "Firefox debug endpoint", "skipped (config unavailable)")
    if not _firefox_debug_configured(cfg):
        return DoctorCheck("ok", "Firefox debug endpoint", "not configured")

    target = _firefox_list_url(cfg.tabs_firefox_debug_url)
    try:
        with request.urlopen(target, timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return DoctorCheck("fail", "Firefox debug endpoint", f"{target} unreachable: {exc}")
    if not isinstance(payload, list):
        return DoctorCheck("fail", "Firefox debug endpoint", f"{target} returned non-list payload")
    return DoctorCheck("ok", "Firefox debug endpoint", f"{target} reachable")


def doctor_checks(config_path: Path | None = None, platform: str | None = None) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = [
        DoctorCheck("ok", "Python executable", sys.executable),
    ]

    config_checks, cfg = _check_config(_config_path(config_path))
    checks.extend(config_checks)

    service_ok, service_detail = service_manager_status(platform)
    checks.append(DoctorCheck("ok" if service_ok else "fail", "Service manager", service_detail))

    opener_ok, opener_detail = link_opener_status(platform)
    opener_check = _status_from_requirement(
        opener_ok,
        opener_detail,
        required=bool(cfg and cfg.auto_open_links),
    )
    checks.append(DoctorCheck(opener_check.status, "Link opener", opener_check.detail))

    clipboard_ok, clipboard_detail = clipboard_tools_status(platform)
    clipboard_check = _status_from_requirement(
        clipboard_ok,
        clipboard_detail,
        required=bool(cfg and cfg.clipboard_mode != "off"),
    )
    checks.append(DoctorCheck(clipboard_check.status, "Clipboard tools", clipboard_check.detail))

    deskflow_ok, deskflow_detail = deskflow_binary_status(platform)
    deskflow_required = bool(cfg and (cfg.deskflow_enabled or cfg.deskflow_role != "off"))
    deskflow_check = _status_from_requirement(
        deskflow_ok,
        deskflow_detail,
        required=deskflow_required,
    )
    checks.append(DoctorCheck(deskflow_check.status, "Deskflow binary", deskflow_check.detail))

    checks.append(_check_firefox_debug(cfg))
    return checks


def doctor_lines(config_path: Path | None = None, platform: str | None = None) -> list[str]:
    return ["Deskbridge doctor", *[check.line() for check in doctor_checks(config_path, platform)]]


def doctor_report(config_path: Path | None = None, platform: str | None = None) -> dict:
    checks = doctor_checks(config_path, platform)
    return {
        "ok": doctor_exit_code(checks) == 0,
        "checks": [
            {"status": check.status, "name": check.name, "detail": check.detail}
            for check in checks
        ],
    }


def doctor_exit_code(checks: list[DoctorCheck]) -> int:
    return 1 if any(check.status == "fail" for check in checks) else 0
