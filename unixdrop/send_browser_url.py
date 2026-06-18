from __future__ import annotations

import argparse
import json
import subprocess
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from unixdrop.config import load_config


CONFIG = load_config()


BROWSER_ALIASES = {
    "safari": "Safari",
    "chrome": "Google Chrome",
    "arc": "Arc",
    "brave": "Brave Browser",
    "chromium": "Chromium",
    "edge": "Microsoft Edge",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
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
]


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


def _resolve_browser_arg(browser: str | None) -> str | None:
    if not browser or browser == "auto":
        return None

    normalized = browser.strip().lower()
    if normalized in BROWSER_ALIASES:
        return BROWSER_ALIASES[normalized]

    for app_name in BROWSER_APP_NAMES:
        if app_name.lower() == normalized:
            return app_name

    supported = ", ".join(sorted(BROWSER_ALIASES))
    raise SystemExit(f"unsupported browser '{browser}'. Supported: {supported}, auto")


def current_browser_context(browser: str | None = None) -> tuple[str, str]:
    selected = _resolve_browser_arg(browser)
    app_names = [selected] if selected else BROWSER_APP_NAMES

    for app_name in app_names:
        if app_name is None:
            continue
        try:
            if not _is_running(app_name):
                continue
            if app_name == "Safari":
                url = _read_safari_url()
            else:
                url = _read_chromium_url(app_name)
            if url:
                return app_name, url
        except SystemExit:
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
) -> None:
    target = (receiver_url or CONFIG.receiver_url).rstrip("/") + "/api/link"
    body = json.dumps({"url": url, "source": source, "no_open": no_open}).encode("utf-8")
    req = request.Request(
        target,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {auth_token or CONFIG.auth_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds or CONFIG.request_timeout_seconds):
            return
    except HTTPError as exc:
        raise SystemExit(
            f"receiver rejected tab send ({exc.code}) at {target}. "
            "Check auth token and receiver logs."
        ) from exc
    except URLError as exc:
        reason = str(exc.reason)
        raise SystemExit(
            f"could not reach peer receiver at {target}: {reason}. "
            "Start/verify receiver with `./deskbridge status` and `./deskbridge health`."
        ) from exc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Send active macOS browser tab to peer receiver")
    parser.add_argument("--browser", default="auto", help="auto, safari, chrome, arc, brave, chromium, edge, vivaldi, opera")
    parser.add_argument("--no-open", action="store_true", help="queue link on peer instead of opening immediately")
    args = parser.parse_args(argv)

    app_name, url = current_browser_context(args.browser)
    if not url:
        raise SystemExit("no active browser url found in supported running browsers")
    if not is_supported_web_url(url):
        label = app_name or "browser"
        raise SystemExit(f"{label} returned a non-web URL: {url}")
    send_url(url, no_open=args.no_open, source="mac-browser-helper")
    print(url)


if __name__ == "__main__":
    main()
