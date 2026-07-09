from __future__ import annotations

import argparse
import json
import subprocess
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urljoin

from unixdrop.config import AppConfig, load_config


DEFAULT_FIREFOX_DEBUG_URL = "http://127.0.0.1:9222"


BROWSER_ALIASES = {
    "safari": "Safari",
    "chrome": "Google Chrome",
    "arc": "Arc",
    "brave": "Brave Browser",
    "chromium": "Chromium",
    "edge": "Microsoft Edge",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "firefox": "Firefox",
    "firefox-developer": "Firefox Developer Edition",
    "firefox-developer-edition": "Firefox Developer Edition",
    "librewolf": "LibreWolf",
}

BROWSER_APP_NAMES = [
    "Safari",
    "Google Chrome",
    "Arc",
    "Brave Browser",
    "Chromium",
    "Microsoft Edge",
    "Vivaldi",
    "Opera",
    "Firefox",
    "Firefox Developer Edition",
    "LibreWolf",
]

FIREFOX_APP_NAMES = {
    "Firefox",
    "Firefox Developer Edition",
    "LibreWolf",
}


def _run_osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "could not read browser url")
    return result.stdout.strip()


def _load_runtime_config(config: AppConfig | None = None) -> AppConfig:
    return config or load_config()


def _is_running(app_name: str) -> bool:
    script = f'''
tell application "System Events"
    return exists application process "{app_name}"
end tell
'''
    return _run_osascript(script).lower() == "true"


def _read_safari_url() -> str:
    script = r'''
tell application "Safari"
    if (count of windows) is 0 then return ""
    try
        return URL of current tab of front window
    on error
        try
            return URL of front document
        on error
            return ""
        end try
    end try
end tell
'''
    return _run_osascript(script)


def _read_chromium_url(app_name: str) -> str:
    script = f'''
tell application "{app_name}"
    if (count of windows) is 0 then return ""
    try
        return URL of active tab of front window
    on error
        return ""
    end try
end tell
'''
    return _run_osascript(script)


def _firefox_list_url(debug_url: str | None = None, config: AppConfig | None = None) -> str:
    cfg = _load_runtime_config(config)
    base = (debug_url or cfg.tabs_firefox_debug_url or DEFAULT_FIREFOX_DEBUG_URL).strip()
    if not base:
        base = DEFAULT_FIREFOX_DEBUG_URL
    if base.endswith("/json/list"):
        return base
    return urljoin(base.rstrip("/") + "/", "json/list")


def _fetch_firefox_targets(debug_url: str | None = None, config: AppConfig | None = None) -> list[dict]:
    target = _firefox_list_url(debug_url, config)
    try:
        with request.urlopen(target, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        reason = str(exc.reason)
        raise SystemExit(
            f"could not reach Firefox debug endpoint at {target}: {reason}. "
            "Start Firefox with remote debugging enabled or pass --firefox-debug-url."
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read Firefox debug endpoint at {target}: {exc}") from exc

    if not isinstance(payload, list):
        raise SystemExit(f"Firefox debug endpoint returned unexpected payload at {target}")
    return [item for item in payload if isinstance(item, dict)]


def _target_url(target: dict) -> str:
    value = target.get("url", "")
    return value if isinstance(value, str) else ""


def _is_page_target(target: dict) -> bool:
    target_type = str(target.get("type", "page")).lower()
    return target_type in {"page", "tab"}


def _is_active_target(target: dict) -> bool:
    for key in ("active", "focused", "selected", "current", "foreground"):
        if target.get(key) is True:
            return True
    info = target.get("targetInfo")
    if isinstance(info, dict):
        for key in ("active", "focused", "selected", "current", "foreground"):
            if info.get(key) is True:
                return True
    return False


def firefox_url_from_targets(targets: list[dict]) -> str:
    candidates = [
        target
        for target in targets
        if _is_page_target(target) and is_supported_web_url(_target_url(target))
    ]
    if not candidates:
        return ""
    active = [target for target in candidates if _is_active_target(target)]
    if len(active) == 1:
        return _target_url(active[0])
    if len(candidates) == 1:
        return _target_url(candidates[0])
    raise SystemExit(
        "Firefox debug endpoint has multiple web tabs but no single active tab marker; "
        "send explicitly with `deskbridge url <url>` or close extra debug tabs."
    )


def _read_firefox_url(debug_url: str | None = None, config: AppConfig | None = None) -> str:
    return firefox_url_from_targets(_fetch_firefox_targets(debug_url, config))


def _resolve_browser_arg(browser: str | None, config: AppConfig | None = None) -> str | None:
    requested = (browser or "auto").strip()
    if requested.lower() == "auto":
        cfg = _load_runtime_config(config)
        configured = str(getattr(cfg, "tabs_default_browser", "auto") or "auto").strip()
        if not configured or configured.lower() == "auto":
            return None
        requested = configured

    normalized = requested.strip().lower()
    if normalized in BROWSER_ALIASES:
        return BROWSER_ALIASES[normalized]

    for app_name in BROWSER_APP_NAMES:
        if app_name.lower() == normalized:
            return app_name

    supported = ", ".join(sorted(BROWSER_ALIASES))
    raise SystemExit(f"unsupported browser '{browser}'. Supported: {supported}, auto")


def current_browser_context(
    browser: str | None = None,
    firefox_debug_url: str | None = None,
    *,
    config: AppConfig | None = None,
) -> tuple[str, str]:
    cfg = _load_runtime_config(config)
    selected = _resolve_browser_arg(browser, cfg)
    app_names = [selected] if selected else BROWSER_APP_NAMES

    for app_name in app_names:
        if app_name is None:
            continue
        try:
            if app_name in FIREFOX_APP_NAMES:
                if selected is None and not _is_running(app_name):
                    continue
                url = _read_firefox_url(firefox_debug_url, cfg)
            else:
                if not _is_running(app_name):
                    continue
                if app_name == "Safari":
                    url = _read_safari_url()
                else:
                    url = _read_chromium_url(app_name)
            if url:
                return app_name, url
        except SystemExit:
            if selected is not None:
                raise
            continue

    return "", ""


def is_supported_web_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def send_url(
    url: str,
    no_open: bool = False,
    *,
    receiver_url: str | None = None,
    auth_token: str | None = None,
    timeout_seconds: int | None = None,
    source: str = "browser-helper",
    config: AppConfig | None = None,
) -> None:
    cfg = config
    if receiver_url is None or auth_token is None or timeout_seconds is None:
        cfg = _load_runtime_config(cfg)
    target = (receiver_url or cfg.receiver_url).rstrip("/") + "/api/link"
    body = json.dumps({"url": url, "source": source, "no_open": no_open}).encode("utf-8")
    req = request.Request(
        target,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {auth_token or cfg.auth_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds or cfg.request_timeout_seconds):
            return
    except HTTPError as exc:
        code = exc.code
        try:
            exc.close()
        except Exception:
            pass
        raise SystemExit(
            f"receiver rejected tab send ({code}) at {target}. "
            "Check auth token and receiver logs."
        ) from None
    except URLError as exc:
        reason = str(exc.reason)
        raise SystemExit(
            f"could not reach peer receiver at {target}: {reason}. "
            "Start/verify receiver with `deskbridge status` and `deskbridge health`."
        ) from exc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Send active browser tab to peer receiver")
    parser.add_argument(
        "--browser",
        default="auto",
        help="auto, safari, chrome, arc, brave, chromium, edge, firefox, firefox-developer, librewolf, vivaldi, opera",
    )
    parser.add_argument(
        "--firefox-debug-url",
        help="Firefox-compatible debug endpoint, defaults to tabs.firefox_debug_url or http://127.0.0.1:9222",
    )
    parser.add_argument("--no-open", action="store_true", help="queue link on peer instead of opening immediately")
    args = parser.parse_args(argv)

    cfg = load_config()
    app_name, url = current_browser_context(args.browser, firefox_debug_url=args.firefox_debug_url, config=cfg)
    if not url:
        raise SystemExit("no active browser url found in supported running browsers")
    if not is_supported_web_url(url):
        label = app_name or "browser"
        raise SystemExit(f"{label} returned a non-web URL: {url}")
    send_url(url, no_open=args.no_open, source="mac-browser-helper", config=cfg)
    print(url)


if __name__ == "__main__":
    main()
