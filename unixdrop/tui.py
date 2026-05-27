from __future__ import annotations

import json
import os
import re
import select
import socket
import subprocess
import sys
import termios
import time
import tty
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from unixdrop.config import DEFAULT_CONFIG_PATH, ENV_CONFIG_PATH, load_config
from unixdrop.health import health_lines
from unixdrop.status import status_lines


HEALTH_RE = re.compile(r"^\[(ok|fail)\]\s+(.+?):\s*(.*)$")
DEFAULT_TUI_SERVER_HOSTS = "100.76.14.117:24800"
DEFAULT_TUI_RECEIVER_URL = "http://100.118.15.70:8765"


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


def _first_endpoint_host(server_hosts: str) -> str:
    for raw in server_hosts.split(","):
        endpoint = raw.strip()
        if not endpoint:
            continue
        if endpoint.startswith("[") and "]" in endpoint:
            return endpoint[1:endpoint.index("]")]
        if endpoint.count(":") == 1:
            return endpoint.split(":", 1)[0].strip()
        return endpoint
    return ""


def _parse_receiver_override(value: str) -> tuple[str, int | None]:
    text = value.strip()
    if not text:
        raise ValueError("empty receiver override")

    if "://" in text:
        parsed = urlparse(text)
        if not parsed.hostname:
            raise ValueError(f"invalid receiver override: {value}")
        return parsed.hostname, parsed.port

    if text.startswith("[") and "]" in text:
        host = text[1:text.index("]")]
        remainder = text[text.index("]") + 1:].strip()
        if remainder.startswith(":"):
            port_value = remainder[1:].strip()
            if port_value:
                return host, int(port_value)
        return host, None

    if text.count(":") == 1:
        host, port_value = text.split(":", 1)
        host = host.strip()
        port_value = port_value.strip()
        if not host:
            raise ValueError(f"invalid receiver override: {value}")
        if port_value:
            return host, int(port_value)
        return host, None

    return text, None


def _sync_receiver_endpoint(
    server_hosts: str,
    receiver_url_override: str | None = None,
) -> tuple[bool, str]:
    host = ""
    override_port: int | None = None
    if receiver_url_override:
        try:
            host, override_port = _parse_receiver_override(receiver_url_override)
        except (ValueError, TypeError):
            return False, f"invalid receiver override: {receiver_url_override}"
    else:
        host = _first_endpoint_host(server_hosts)
        if not host:
            return False, "could not derive receiver host from entered endpoints"

    config_path = Path(os.environ.get(ENV_CONFIG_PATH, str(DEFAULT_CONFIG_PATH))).expanduser()
    if not config_path.exists():
        return False, f"unixdrop config missing: {config_path}"

    try:
        raw = json.loads(config_path.read_text())
    except Exception as exc:
        return False, f"failed to read unixdrop config: {exc}"

    receiver = raw.get("receiver") if isinstance(raw.get("receiver"), dict) else {}
    receiver_url = str(raw.get("receiver_url", "")).strip()

    port = receiver.get("port", 8765)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8765
    if receiver_url:
        parsed = urlparse(receiver_url)
        if parsed.port:
            port = parsed.port
    if override_port is not None:
        port = override_port

    receiver["host"] = host
    receiver["port"] = port
    raw["receiver"] = receiver
    raw["receiver_url"] = f"http://{host}:{port}"

    try:
        config_path.write_text(json.dumps(raw, indent=2) + "\n")
    except Exception as exc:
        return False, f"failed to write unixdrop config: {exc}"

    return True, f"receiver endpoint set to http://{host}:{port}"


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


def _local_tcp_open(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((host, int(port))) == 0
    except Exception:
        return False


def _start_linux_receiver_now() -> tuple[bool, str]:
    if not sys.platform.startswith("linux"):
        return True, "linux receiver autostart skipped (non-linux)"

    cfg = load_config()
    receiver_port = int(cfg.port)
    if _local_tcp_open("127.0.0.1", receiver_port):
        return True, f"linux receiver already listening on 127.0.0.1:{receiver_port}"

    script = _project_dir() / "scripts" / "run_linux_receiver.sh"
    if not script.exists():
        return False, f"linux receiver script missing: {script}"
    if not os.access(script, os.X_OK):
        return False, f"linux receiver script not executable: {script}"
    try:
        proc = subprocess.Popen(
            [str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, f"linux receiver start requested (pid={proc.pid})"
    except Exception as exc:
        return False, f"linux receiver start failed: {exc}"


def _restart_deskflow_client_now() -> tuple[bool, str]:
    cfg = load_config()
    script = cfg.deskflow_linux_start_script
    if not script.exists():
        return False, f"deskflow client start script missing: {script}"
    if not os.access(script, os.X_OK):
        return False, f"deskflow client start script not executable: {script}"

    # Ensure endpoint changes apply immediately by dropping stale client processes first.
    for command in (["pkill", "-f", "deskflow-client"], ["pkill", "-f", "deskflow-core.*client"]):
        try:
            subprocess.run(command, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            break

    try:
        proc = subprocess.Popen([str(script)])
        return True, f"deskflow client restart requested (pid={proc.pid})"
    except Exception as exc:
        return False, f"deskflow client restart failed: {exc}"


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
    receiver_ok, receiver_detail = _start_linux_receiver_now()
    if sys.platform.startswith("linux"):
        message = receiver_detail if receiver_ok else f"warning: {receiver_detail}"
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
                    entered = DEFAULT_TUI_SERVER_HOSTS
                    receiver_override = DEFAULT_TUI_RECEIVER_URL
                    prefix = "empty input -> using defaults"
                else:
                    receiver_override = None
                    prefix = "saved endpoints"
                receiver_input = _prompt_line(
                    "Linux receiver IP/host for UnixDrop (blank=same as first server host): "
                ).strip()
                if receiver_input:
                    receiver_override = receiver_input
                ok, detail = _apply_client_server_hosts(entered)
                recv_ok, recv_detail = _sync_receiver_endpoint(entered, receiver_override)
                message = detail
                if ok:
                    _, start_detail = _restart_deskflow_client_now()
                    message = f"{prefix}: {entered} | {recv_detail} | {start_detail}"
                    if not recv_ok:
                        message = f"{prefix}: {entered} | warning: {recv_detail} | {start_detail}"
                else:
                    message = f"{detail} | {recv_detail}"
                    if not recv_ok:
                        message = f"{detail} | warning: {recv_detail}"
                continue
            if key.lower() == "d":
                ok, detail = _start_deskflow_now()
                message = detail if ok else f"error: {detail}"
