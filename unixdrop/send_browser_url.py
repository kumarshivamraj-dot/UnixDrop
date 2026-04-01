from __future__ import annotations

import json
import subprocess
from urllib import request

from unixdrop.config import load_config


CONFIG = load_config()


APPLE_SCRIPT = r'''
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
end tell

if frontApp is "Safari" then
    tell application "Safari"
        return URL of front document
    end tell
end if

if frontApp is "Google Chrome" then
    tell application "Google Chrome"
        return URL of active tab of front window
    end tell
end if

if frontApp is "Arc" then
    tell application "Arc"
        return URL of active tab of front window
    end tell
end if

return ""
'''


def current_browser_url() -> str:
    result = subprocess.run(
        ["osascript", "-e", APPLE_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "could not read active browser url")
    return result.stdout.strip()


def send_url(url: str) -> None:
    body = json.dumps({"url": url, "source": "mac-browser-helper"}).encode("utf-8")
    req = request.Request(
        CONFIG.receiver_url.rstrip("/") + "/api/link",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {CONFIG.auth_token}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=CONFIG.request_timeout_seconds):
        return


def main() -> None:
    url = current_browser_url()
    if not url:
        raise SystemExit("no active browser url found")
    send_url(url)
    print(url)


if __name__ == "__main__":
    main()
