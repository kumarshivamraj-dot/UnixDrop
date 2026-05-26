from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import termios
import time
import tty
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from unixdrop.config import load_config
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


def _prompt_line(prompt: str) -> str:
    if not sys.stdin.isatty():
        return ""
    fd = sys.stdin.fileno()
    attrs = termios.tcgetattr(fd)
    attrs[3] |= termios.ICANON | termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
    try:
        print("")
        return input(prompt)
    finally:
        tty.setcbreak(fd)


def _project_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_client_name() -> str:
    result = subprocess.run(["hostname"], capture_output=True, text=True, check=False)
    name = result.stdout.strip()
    return name or "deskflow-client"


def _run_command(command: list[str]) -> tuple[bool, str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return True, "ok"
    detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    return False, detail


def _apply_client_server_hosts(server_hosts: str) -> tuple[bool, str]:
    project_dir = _project_dir()
    script = project_dir / "scripts" / "configure_deskflow.sh"
    command = [
        str(script),
        "--role",
        "client",
        "--server-hosts",
        server_hosts,
        "--client-name",
        _default_client_name(),
        "--autostart",
    ]
    ok, detail = _run_command(command)
    if ok:
        return True, f"saved endpoints: {server_hosts}"
    return False, f"failed to save endpoints: {detail}"


def _start_deskflow_now() -> tuple[bool, str]:
    cfg = load_config()
    script = cfg.deskflow_linux_start_script
    if sys.platform == "darwin":
        script = cfg.deskflow_mac_start_script
    if not script.exists():
        return False, f"deskflow start script missing: {script}"
    if not os.access(script, os.X_OK):
        return False, f"deskflow start script not executable: {script}"
    try:
        proc = subprocess.Popen([str(script)])
        return True, f"deskflow start requested (pid={proc.pid})"
    except Exception as exc:
        return False, f"deskflow start failed: {exc}"


def _render(
    snapshot_time: str,
    checks: list[tuple[bool, str, str]],
    status: dict[str, str],
    interval: float,
    message: str,
) -> None:
    _clear()
    print(_cyan("Deskbridge TUI"))
    print(f"Updated: {snapshot_time} | refresh={interval:.1f}s | keys: q quit, e edit endpoints, d start deskflow")
    print("")

    receiver = status.get("linux receiver reachable", "unknown")
    clipboard_mode = status.get("clipboard_mode", "unknown")
    deskflow_hint = "managed by unixdrop" if status.get("mac agent running", "no").startswith("yes") else "unknown"
    print(f"Receiver: {receiver}")
    print(f"Clipboard mode: {clipboard_mode}")
    print(f"Deskflow: {deskflow_hint}")
    print(f"Message: {message}")
    print("")

    print("Component checks:")
    for ok, name, detail in checks:
        label = _green("OK  ") if ok else _red("FAIL")
        print(f"  {label}  {name}  |  {detail}")


def run_tui(interval_seconds: float = 3.0, once: bool = False) -> int:
    interval = max(interval_seconds, 0.5)
    message = "ready"
    with _raw_stdin():
        while True:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            health = _parse_health(health_lines())
            status = _collect_status_map(status_lines())
            _render(now, health, status, interval, message)
            if once:
                return 0
            key = _read_key(interval)
            if key and key.lower() == "q":
                return 0
            if not key:
                continue
            if key.lower() == "e":
                entered = _prompt_line("Server endpoints (lan:24800,tailscale:24800): ").strip()
                if not entered:
                    message = "endpoint update cancelled"
                    continue
                ok, detail = _apply_client_server_hosts(entered)
                message = detail
                if ok:
                    start_ok, start_detail = _start_deskflow_now()
                    if not start_ok:
                        message = f"{detail} | {start_detail}"
                continue
            if key.lower() == "d":
                ok, detail = _start_deskflow_now()
                message = detail if ok else f"error: {detail}"
