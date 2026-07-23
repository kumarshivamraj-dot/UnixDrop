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
from collections import deque
from datetime import datetime
from pathlib import Path
from statistics import pstdev
from urllib.parse import urlparse

from unixdrop.config import DEFAULT_CONFIG_PATH, ENV_CONFIG_PATH, deskflow_start_script, load_config
from unixdrop.deskflow_setup import main as deskflow_setup_main
from unixdrop.health import health_lines
from unixdrop.status import status_lines


HEALTH_RE = re.compile(r"^\[(ok|fail)\]\s+(.+?):\s*(.*)$")
LATENCY_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*ms$", re.IGNORECASE)


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


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


def _cyan(text: str) -> str:
    return f"\033[36m{text}\033[0m"


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


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


def _default_client_name() -> str:
    result = subprocess.run(["hostname"], capture_output=True, text=True, check=False)
    name = result.stdout.strip()
    return name or "deskflow-client"


def _parse_latency_ms(value: str) -> float | None:
    matched = LATENCY_RE.match(value.strip())
    if not matched:
        return None
    try:
        return float(matched.group("value"))
    except ValueError:
        return None


def _format_latency_badge(latency_ms: float | None) -> str:
    if latency_ms is None:
        return _yellow("latency unknown")
    if latency_ms < 20:
        return _green(f"latency {latency_ms:.0f} ms")
    if latency_ms < 60:
        return _yellow(f"latency {latency_ms:.0f} ms")
    return _red(f"latency {latency_ms:.0f} ms")


def _format_jitter_badge(samples: list[float]) -> str:
    if len(samples) < 2:
        return _cyan("jitter n/a")
    jitter_ms = pstdev(samples)
    if jitter_ms < 5:
        return _green(f"jitter {jitter_ms:.0f} ms")
    if jitter_ms < 20:
        return _yellow(f"jitter {jitter_ms:.0f} ms")
    return _red(f"jitter {jitter_ms:.0f} ms")


def _top_summary(status: dict[str, str], latency_samples: list[float]) -> str:
    receiver = status.get("peer receiver reachable", "unknown")
    clipboard_mode = status.get("clipboard_mode", "unknown")
    deskflow_enabled = status.get("deskflow_enabled", "no")
    deskflow_role = status.get("deskflow_role", "off")
    peer_hostname = status.get("peer hostname", "unknown")
    latency = _parse_latency_ms(status.get("peer receiver latency", "unknown"))
    receiver_badge = _green("receiver up") if receiver.startswith("yes") else _red("receiver down")
    deskflow_badge = (
        _green(f"deskflow {deskflow_role}")
        if deskflow_enabled.startswith("yes") and deskflow_role != "off"
        else _yellow("deskflow off")
    )
    return " | ".join(
        (
            receiver_badge,
            _format_latency_badge(latency),
            _format_jitter_badge(latency_samples),
            _cyan(f"clipboard {clipboard_mode}"),
            deskflow_badge,
            _cyan(f"peer {peer_hostname}"),
        )
    )


def _panel_width(lines: list[str], title: str) -> int:
    content_width = max([len(title)] + [len(line) for line in lines])
    return max(content_width + 2, 28)


def _panel_lines(title: str, body: list[str]) -> list[str]:
    width = _panel_width(body, title)
    top = f"+-{title.ljust(width - 2, '-')}-+"
    lines = [top]
    for line in body:
        lines.append(f"| {line.ljust(width - 4)} |")
    lines.append(f"+{'-' * (width - 2)}+")
    return lines


def _terminal_width(default: int = 88) -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return default


def _truncate_middle(value: str, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    left = (width - 3) // 2
    right = width - 3 - left
    return f"{text[:left]}...{text[-right:]}"


def _drop_queue_summary(pending_value: str) -> str:
    text = str(pending_value).strip() or "unknown"
    try:
        pending_count = int(text)
    except ValueError:
        return text
    if pending_count == 0:
        return "idle - no files waiting"
    if pending_count == 1:
        return "1 file waiting"
    return f"{pending_count} files waiting"


def _drop_panel_lines(status: dict[str, str], width: int | None = None) -> list[str]:
    panel_width = width or min(max(_terminal_width(), 72), 104)
    title = "drop to peer"
    panel_width = max(panel_width, 56, len(title) + 4)
    label_width = 11
    value_width = panel_width - label_width - 6
    rows = [
        ("drop folder", status.get("local drop folder", "unknown")),
        ("local inbox", status.get("local inbox", "unknown")),
        ("queue", _drop_queue_summary(status.get("pending files in drop folder", "unknown"))),
        ("last upload", status.get("last upload result", "none")),
    ]

    lines = [f"+-{title.ljust(panel_width - 4, '-')}-+"]
    for label, raw_value in rows:
        value = _truncate_middle(str(raw_value), value_width)
        lines.append(f"| {label:<{label_width}}  {value:<{value_width}} |")
    lines.append(f"+{'-' * (panel_width - 2)}+")
    return lines


def _center_badges(badges: list[str], width: int) -> str:
    text = "  ".join(badges)
    if len(text) >= width:
        return text
    padding = width - len(text)
    left = padding // 2
    right = padding - left
    return f"{' ' * left}{text}{' ' * right}"


def _run_command(command: list[str]) -> tuple[bool, str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return True, "ok"
    detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    return False, detail


def _run_deskflow_setup(command_args: list[str]) -> tuple[bool, str]:
    try:
        result = deskflow_setup_main(command_args)
    except SystemExit as exc:
        code = exc.code
        if code in (None, 0):
            return True, "ok"
        return False, str(exc) or f"exit code {code}"
    except Exception as exc:
        return False, str(exc)
    if result in (None, 0):
        return True, "ok"
    return False, f"exit code {result}"


def _apply_client_server_hosts(server_hosts: str) -> tuple[bool, str]:
    endpoints = server_hosts.strip()
    command = [
        "--role",
        "client",
    ]
    if endpoints:
        command.extend(["--server-hosts", endpoints])
    command.extend(
        [
            "--client-name",
            _default_client_name(),
            "--autostart",
        ]
    )
    ok, detail = _run_deskflow_setup(command)
    if ok:
        if endpoints:
            return True, f"saved endpoints: {endpoints}"
        return True, "configured Deskflow client for LAN discovery"
    return False, f"failed to save endpoints: {detail}"


def _update_quick_setup_config(role: str, peer_name: str = "") -> tuple[bool, str]:
    config_path = Path(os.environ.get(ENV_CONFIG_PATH, str(DEFAULT_CONFIG_PATH))).expanduser()
    if not config_path.exists():
        return False, f"unixdrop config missing: {config_path}"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        clipboard = raw.get("clipboard") if isinstance(raw.get("clipboard"), dict) else {}
        clipboard["mode"] = "two_way"
        raw["clipboard"] = clipboard
        deskflow = raw.get("deskflow") if isinstance(raw.get("deskflow"), dict) else {}
        deskflow.update(
            {
                "enabled": True,
                "role": role,
                "server_start_script": "~/.config/deskflow/start-deskflow-server.sh",
                "client_start_script": "~/.config/deskflow/start-deskflow-client.sh",
            }
        )
        if peer_name.strip():
            deskflow["peer_name"] = peer_name.strip()
        raw["deskflow"] = deskflow
        config_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return False, f"failed to update unixdrop config: {exc}"
    return True, f"saved {role} role and enabled two-way clipboard"


def _saved_deskflow_peer_name() -> str:
    config_path = Path(os.environ.get(ENV_CONFIG_PATH, str(DEFAULT_CONFIG_PATH))).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    deskflow = raw.get("deskflow") if isinstance(raw.get("deskflow"), dict) else {}
    return str(deskflow.get("peer_name", "")).strip()


def _quick_setup_deskflow(peer_hostname: str = "") -> tuple[bool, str]:
    if sys.platform == "darwin":
        role = "server"
        client_name = peer_hostname.strip()
        if not client_name or client_name == "unknown":
            client_name = _saved_deskflow_peer_name()
        if not client_name:
            return False, "peer name is not available yet; start UnixDrop on the peer client machine first"
        command = [
            "--role",
            role,
            "--client-name",
            client_name,
            "--direction",
            "right",
        ]
    elif sys.platform.startswith("linux"):
        role = "client"
        command = [
            "--role",
            role,
            "--client-name",
            _default_client_name(),
        ]
    else:
        return False, f"automatic setup is unsupported on {sys.platform}"

    disabled, disable_detail = _disable_standalone_deskflow_autostarts()
    if not disabled:
        return False, disable_detail
    stopped, stop_detail = _stop_deskflow_processes()
    if not stopped:
        return False, stop_detail
    ok, detail = _run_deskflow_setup(command)
    if not ok:
        return False, f"Deskflow setup failed: {detail}"
    config_ok, config_detail = _update_quick_setup_config(
        role, client_name if role == "server" else ""
    )
    if not config_ok:
        return False, config_detail
    started, start_detail = _start_deskflow_now()
    if not started:
        return False, f"{config_detail}; {start_detail}"
    return True, f"ready as {role}; {start_detail}"


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
            return True, "receiver endpoint unchanged (LAN discovery only)"

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
    role = cfg.deskflow_role
    role_was_off = role == "off"
    if role_was_off:
        default_role = _default_deskflow_role_for_platform()
        if default_role is None:
            return False, f"unsupported platform: {sys.platform}"
        role = default_role
        script = _deskflow_script_for_role(cfg, role)
    else:
        script = deskflow_start_script(cfg, sys.platform)
        if script is None:
            return False, "deskflow role is off"

    running_role = _current_deskflow_role()
    if running_role and running_role == role:
        if role_was_off:
            role_ok, role_detail = _set_deskflow_role(role)
            if not role_ok:
                return False, role_detail
        return True, f"deskflow {running_role} already running; no action needed"
    if not script.exists():
        return False, f"deskflow {role} start script missing: {script}"
    if not os.access(script, os.X_OK):
        return False, f"deskflow {role} start script not executable: {script}"
    try:
        proc = subprocess.Popen([str(script)])
        time.sleep(0.4)
        return_code = proc.poll()
        if return_code is not None:
            if sys.platform == "darwin":
                return False, (
                    f"deskflow exited with code {return_code}; allow Deskflow in "
                    "System Settings > Privacy & Security > Accessibility"
                )
            return False, f"deskflow exited with code {return_code}; check its service log"
        if role_was_off:
            role_ok, role_detail = _set_deskflow_role(role)
            if not role_ok:
                return False, role_detail
            return True, f"deskflow {role} start requested (pid={proc.pid}); {role_detail}"
        return True, f"deskflow {role} start requested (pid={proc.pid})"
    except Exception as exc:
        return False, f"deskflow start failed: {exc}"


def _open_drop_folder_now() -> tuple[bool, str]:
    cfg = load_config()
    cfg.drop_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        command = ["open", str(cfg.drop_dir)]
    elif sys.platform.startswith("linux"):
        command = ["xdg-open", str(cfg.drop_dir)]
    else:
        return False, f"unsupported platform: {sys.platform}"
    try:
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f"opened drop folder: {cfg.drop_dir} (pid={proc.pid})"
    except FileNotFoundError:
        return False, f"folder opener not found for platform: {sys.platform}"
    except Exception as exc:
        return False, f"open drop folder failed: {exc}"


def _local_tcp_open(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((host, int(port))) == 0
    except Exception:
        return False


def _start_local_receiver_now() -> tuple[bool, str]:
    cfg = load_config()
    receiver_port = int(cfg.port)
    if _local_tcp_open("127.0.0.1", receiver_port):
        return True, f"local receiver already listening on 127.0.0.1:{receiver_port}"

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "unixdrop.linux_service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, f"local receiver start requested (pid={proc.pid})"
    except Exception as exc:
        return False, f"local receiver start failed: {exc}"


def _start_linux_receiver_now() -> tuple[bool, str]:
    return _start_local_receiver_now()


def _restart_deskflow_client_now() -> tuple[bool, str]:
    cfg = load_config()
    script = cfg.deskflow_client_start_script
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


def _deskflow_role_running(role: str) -> bool:
    patterns = {
        "server": ("deskflow-server", "deskflow-core.*server"),
        "client": ("deskflow-client", "deskflow-core.*client"),
    }
    role_patterns = list(patterns[role])
    if role == "server" and sys.platform == "darwin":
        role_patterns.append("deskflow-core")
    if role == "client" and sys.platform.startswith("linux"):
        role_patterns.append("deskflow-core")
    for pattern in role_patterns:
        try:
            result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return False
        if result.returncode == 0:
            return True
    return False


def _current_deskflow_role() -> str | None:
    if _deskflow_role_running("server"):
        return "server"
    if _deskflow_role_running("client"):
        return "client"
    return None


def _configured_deskflow_script_for_role(role: str) -> Path:
    cfg = load_config()
    if role == "server":
        return cfg.deskflow_mac_start_script
    return cfg.deskflow_linux_start_script


def _stop_deskflow_processes() -> tuple[bool, str]:
    stop_patterns = (
        "deskflow-server",
        "deskflow-client",
        "deskflow-core.*server",
        "deskflow-core.*client",
        "deskflow-core",
    )
    for pattern in stop_patterns:
        try:
            result = subprocess.run(["pkill", "-KILL", "-f", pattern], capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return False, "process killer not found: pkill"
        if result.returncode not in (0, 1):
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            return False, f"failed to stop Deskflow process matching {pattern}: {detail}"
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _current_deskflow_role():
            return True, "stopped existing Deskflow processes"
        time.sleep(0.1)
    if _current_deskflow_role():
        return False, "Deskflow did not stop within 2 seconds"
    return True, "stopped existing Deskflow processes"


def _disable_standalone_deskflow_autostarts() -> tuple[bool, str]:
    if sys.platform == "darwin":
        for role in ("server", "client"):
            plist = Path(f"~/Library/LaunchAgents/com.unixdrop.deskflow.{role}.plist").expanduser()
            if not plist.exists():
                continue
            result = subprocess.run(
                ["launchctl", "unload", str(plist)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode not in (0, 3):
                detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                return False, f"failed to disable old Deskflow {role} agent: {detail}"
        return True, "disabled old standalone Deskflow agents"

    if sys.platform.startswith("linux"):
        for role in ("server", "client"):
            result = subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"deskflow-{role}.service"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 and "does not exist" not in result.stderr and "not loaded" not in result.stderr:
                detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                return False, f"failed to disable old Deskflow {role} service: {detail}"
        return True, "disabled old standalone Deskflow services"

    return False, f"unsupported platform: {sys.platform}"


def _set_deskflow_off() -> tuple[bool, str]:
    config_path = Path(os.environ.get(ENV_CONFIG_PATH, str(DEFAULT_CONFIG_PATH))).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        deskflow = raw.get("deskflow") if isinstance(raw.get("deskflow"), dict) else {}
        deskflow["enabled"] = False
        deskflow["role"] = "off"
        raw["deskflow"] = deskflow
        config_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return False, f"failed to disable Deskflow in config: {exc}"
    return True, "Deskflow disabled in config"


def _set_deskflow_role(role: str) -> tuple[bool, str]:
    config_path = Path(os.environ.get(ENV_CONFIG_PATH, str(DEFAULT_CONFIG_PATH))).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        deskflow = raw.get("deskflow") if isinstance(raw.get("deskflow"), dict) else {}
        deskflow["enabled"] = True
        deskflow["role"] = role
        raw["deskflow"] = deskflow
        config_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return False, f"failed to enable Deskflow {role} in config: {exc}"
    return True, f"Deskflow enabled as {role}"


def _default_deskflow_role_for_platform() -> str | None:
    if sys.platform == "darwin":
        return "server"
    if sys.platform.startswith("linux"):
        return "client"
    return None


def _deskflow_script_for_role(cfg, role: str) -> Path:
    if role == "server":
        return cfg.deskflow_server_start_script
    return cfg.deskflow_client_start_script


def _stop_all_now() -> tuple[bool, str]:
    errors: list[str] = []

    config_ok, config_detail = _set_deskflow_off()
    if not config_ok:
        errors.append(config_detail)

    if sys.platform == "darwin":
        plists = [
            Path("~/Library/LaunchAgents/com.unixdrop.agent.plist").expanduser(),
            Path("~/Library/LaunchAgents/com.unixdrop.deskflow.server.plist").expanduser(),
            Path("~/Library/LaunchAgents/com.unixdrop.deskflow.client.plist").expanduser(),
        ]
        for plist in plists:
            if not plist.exists():
                continue
            result = subprocess.run(
                ["launchctl", "unload", str(plist)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode not in (0, 3):
                detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                errors.append(f"could not unload {plist.name}: {detail}")
    elif sys.platform.startswith("linux"):
        for service in (
            "unixdrop-receiver.service",
            "deskflow-server.service",
            "deskflow-client.service",
        ):
            result = subprocess.run(
                ["systemctl", "--user", "disable", "--now", service],
                capture_output=True,
                text=True,
                check=False,
            )
            stderr = result.stderr.strip().lower()
            if result.returncode != 0 and "does not exist" not in stderr and "not loaded" not in stderr:
                errors.append(f"could not stop {service}: {result.stderr.strip() or result.stdout.strip()}")
    else:
        errors.append(f"unsupported platform: {sys.platform}")

    patterns = (
        "deskflow-server",
        "deskflow-client",
        "deskflow-core.*server",
        "deskflow-core.*client",
        "unixdrop/discovery.py.*serve",
        "-m unixdrop.linux_service",
        "-m unixdrop.node",
    )
    for pattern in patterns:
        try:
            result = subprocess.run(
                ["pkill", "-KILL", "-f", "--", pattern],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            errors.append("pkill is not available")
            break
        if result.returncode not in (0, 1):
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            errors.append(f"could not stop process matching {pattern}: {detail}")

    if errors:
        return False, "; ".join(errors)
    return True, "UnixDrop, Deskflow, discovery, and receivers stopped"


def _swap_deskflow_role_now() -> tuple[bool, str]:
    current_role = _current_deskflow_role()
    if current_role is None:
        current_role = "server" if sys.platform == "darwin" else "client"
    next_role = "client" if current_role == "server" else "server"
    try:
        script = _configured_deskflow_script_for_role(next_role)
    except Exception as exc:
        return False, f"failed to load Deskflow config: {exc}"
    if not script.exists():
        return False, f"deskflow {next_role} start script missing: {script}"
    if not os.access(script, os.X_OK):
        return False, f"deskflow {next_role} start script not executable: {script}"

    stopped, stop_detail = _stop_deskflow_processes()
    if not stopped:
        return False, stop_detail

    try:
        proc = subprocess.Popen([str(script)])
        return True, f"deskflow switched {current_role} -> {next_role} (pid={proc.pid})"
    except Exception as exc:
        return False, f"deskflow role switch failed: {exc}"


def _render(
    snapshot_time: str,
    checks: list[tuple[bool, str, str]],
    status: dict[str, str],
    latency_samples: list[float],
    interval: float,
    message: str,
) -> None:
    _clear()
    print(_cyan("Deskbridge TUI"))
    print(f"Updated: {snapshot_time} | refresh={interval:.1f}s")
    summary_line = _top_summary(status, latency_samples)
    summary_width = max(78, len(summary_line) + 4)
    summary_body = [
        _center_badges(
            [
                _green("receiver up") if status.get("peer receiver reachable", "unknown").startswith("yes") else _red("receiver down"),
                _format_latency_badge(_parse_latency_ms(status.get("peer receiver latency", "unknown"))),
                _format_jitter_badge(latency_samples),
            ],
            summary_width - 4,
        ),
        _center_badges(
            [
                _cyan(f"clipboard {status.get('clipboard_mode', 'unknown')}"),
                _cyan(f"peer {status.get('peer hostname', 'unknown')}"),
                _green(f"deskflow {status.get('deskflow_role', 'off')}")
                if status.get("deskflow_enabled", "no").startswith("yes") and status.get("deskflow_role", "off") != "off"
                else _yellow("deskflow off"),
            ],
            summary_width - 4,
        ),
    ]
    for line in _panel_lines("connection summary", summary_body):
        print(line)
    print("keys: s setup, q close UI, x stop all, e endpoints, d deskflow, r reverse, o drop")
    print("")

    receiver = status.get("peer receiver reachable", "unknown")
    receiver_latency = status.get("peer receiver latency", "unknown")
    clipboard_mode = status.get("clipboard_mode", "unknown")
    deskflow_enabled = status.get("deskflow_enabled", "no")
    deskflow_role = status.get("deskflow_role", "off")
    deskflow_hint = (
        f"{deskflow_role} (managed by unixdrop)" if deskflow_enabled.startswith("yes") else "managed by unixdrop"
    )
    print(f"Receiver: {receiver}")
    print(f"Latency: {receiver_latency}")
    print(f"Clipboard mode: {clipboard_mode}")
    print(f"Deskflow: {deskflow_hint}")
    print(f"Peer hostname: {status.get('peer hostname', 'unknown')}")
    print(f"Message: {message}")
    print("")

    for line in _drop_panel_lines(status):
        print(line)
    print("")

    print(_dim("Component checks"))
    for ok, name, detail in checks:
        label = _green("OK  ") if ok else _red("FAIL")
        print(f"  {label}  {name:<28} |  {detail}")


def run_tui(interval_seconds: float = 3.0, once: bool = False) -> int:
    interval = max(interval_seconds, 0.5)
    message = "ready"
    latency_samples = deque(maxlen=8)
    receiver_ok, receiver_detail = _start_local_receiver_now()
    message = receiver_detail if receiver_ok else f"warning: {receiver_detail}"
    with _raw_stdin():
        while True:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            health = _parse_health(health_lines())
            status = _collect_status_map(status_lines())
            latency_ms = _parse_latency_ms(status.get("peer receiver latency", "unknown"))
            if latency_ms is not None:
                latency_samples.append(latency_ms)
            _render(now, health, status, list(latency_samples), interval, message)
            if once:
                return 0
            key = _read_key(interval)
            if key and key.lower() == "q":
                return 0
            if not key:
                continue
            if key.lower() == "e":
                entered = _prompt_line("Server endpoints (blank=LAN discovery, or lan:24800,tailnet:24800): ").strip()
                if not entered:
                    receiver_override = None
                    prefix = "using LAN discovery"
                else:
                    receiver_override = None
                    prefix = "saved endpoints"
                endpoint_label = entered or "LAN discovery"
                receiver_input = _prompt_line(
                    "Peer receiver IP/host for UnixDrop (blank=same as first server host; discovery leaves unchanged): "
                ).strip()
                if receiver_input:
                    receiver_override = receiver_input
                ok, detail = _apply_client_server_hosts(entered)
                recv_ok, recv_detail = _sync_receiver_endpoint(entered, receiver_override)
                message = detail
                if ok:
                    _, start_detail = _restart_deskflow_client_now()
                    message = f"{prefix}: {endpoint_label} | {recv_detail} | {start_detail}"
                    if not recv_ok:
                        message = f"{prefix}: {endpoint_label} | warning: {recv_detail} | {start_detail}"
                else:
                    message = f"{detail} | {recv_detail}"
                    if not recv_ok:
                        message = f"{detail} | warning: {recv_detail}"
                continue
            if key.lower() == "s":
                ok, detail = _quick_setup_deskflow(status.get("peer hostname", ""))
                message = detail if ok else f"error: {detail}"
                continue
            if key.lower() == "x":
                ok, detail = _stop_all_now()
                _clear()
                print(_green(detail) if ok else _red(f"Shutdown incomplete: {detail}"))
                return 0 if ok else 1
            if key.lower() == "d":
                ok, detail = _start_deskflow_now()
                message = detail if ok else f"error: {detail}"
                continue
            if key.lower() == "r":
                ok, detail = _swap_deskflow_role_now()
                message = detail if ok else f"error: {detail}"
                continue
            if key.lower() == "o":
                ok, detail = _open_drop_folder_now()
                message = detail if ok else f"error: {detail}"
