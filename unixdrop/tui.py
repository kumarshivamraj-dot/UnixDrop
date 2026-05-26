from __future__ import annotations

import re
import select
import sys
import termios
import time
import tty
from contextlib import contextmanager
from datetime import datetime

from unixdrop.health import health_lines
from unixdrop.status import status_lines


HEALTH_RE = re.compile(r"^\[(ok|fail)\]\s+(.+?):\s*(.*)$")


def _parse_health(lines: list[str]) -> list[tuple[bool, str, str]]:
    rows: list[tuple[bool, str, str]] = []
    for line in lines:
        matched = HEALTH_RE.match(line.strip())
        if not matched:
            continue
        ok = matched.group(1) == "ok"
        rows.append((ok, matched.group(2), matched.group(3)))
    return rows


def _collect_status_map(lines: list[str]) -> dict[str, str]:
    details: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        details[key.strip().lower()] = value.strip()
    return details


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def _cyan(text: str) -> str:
    return f"\033[36m{text}\033[0m"


def _clear() -> None:
    print("\033[2J\033[H", end="")


@contextmanager
def _raw_stdin():
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def _read_key(timeout_seconds: float) -> str | None:
    if not sys.stdin.isatty():
        time.sleep(timeout_seconds)
        return None
    ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
    if not ready:
        return None
    return sys.stdin.read(1)


def _render(snapshot_time: str, checks: list[tuple[bool, str, str]], status: dict[str, str], interval: float) -> None:
    _clear()
    print(_cyan("Deskbridge TUI"))
    print(f"Updated: {snapshot_time} | refresh={interval:.1f}s | press q to quit")
    print("")

    receiver = status.get("linux receiver reachable", "unknown")
    clipboard_mode = status.get("clipboard_mode", "unknown")
    deskflow_hint = "managed by unixdrop" if status.get("mac agent running", "no").startswith("yes") else "unknown"
    print(f"Receiver: {receiver}")
    print(f"Clipboard mode: {clipboard_mode}")
    print(f"Deskflow: {deskflow_hint}")
    print("")

    print("Component checks:")
    for ok, name, detail in checks:
        label = _green("OK  ") if ok else _red("FAIL")
        print(f"  {label}  {name}  |  {detail}")


def run_tui(interval_seconds: float = 3.0, once: bool = False) -> int:
    interval = max(interval_seconds, 0.5)
    with _raw_stdin():
        while True:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            health = _parse_health(health_lines())
            status = _collect_status_map(status_lines())
            _render(now, health, status, interval)
            if once:
                return 0
            key = _read_key(interval)
            if key and key.lower() == "q":
                return 0
