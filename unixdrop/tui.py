from __future__ import annotations

import json
import os
import re
import select
import secrets
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
MAX_EVENT_DETAIL = 220
DESKFLOW_LOG_PROBLEM_RE = re.compile(
    r"(already connected|cannot|connection refused|error|failed|incompatible|new client is unresponsive|protocol error|refused|timed out)",
    re.IGNORECASE,
)


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


def _event_line(level: str, detail: str) -> str:
    timestamp = datetime.now().strftime("%H:%M:%S")
    clean = " ".join(str(detail).split())
    if len(clean) > MAX_EVENT_DETAIL:
        clean = clean[: MAX_EVENT_DETAIL - 3] + "..."
    return f"{timestamp} {level.upper()}: {clean}"


def _record_event(events: deque[str], level: str, detail: str) -> None:
    events.append(_event_line(level, detail))


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


def _prompt_line_cooked(prompt: str) -> str:
    if not sys.stdin.isatty():
        return ""
    return input(prompt)


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


def _active_config_path() -> Path:
    return Path(os.environ.get(ENV_CONFIG_PATH, str(DEFAULT_CONFIG_PATH))).expanduser()


def _read_config_payload() -> tuple[dict, str | None]:
    config_path = _active_config_path()
    if not config_path.exists():
        return {}, f"unixdrop config missing: {config_path}"
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, f"failed to read unixdrop config: {exc}"
    if not isinstance(loaded, dict):
        return {}, f"unixdrop config must be a JSON object: {config_path}"
    return loaded, None


def _write_config_payload(payload: dict) -> None:
    config_path = _active_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_name(f".{config_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, config_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _default_tui_config_payload() -> dict:
    inbox_dir = "~/UnixDrop/Inbox"
    drop_dir = "~/UnixDrop/Drop"
    return {
        "auth_token": secrets.token_urlsafe(32),
        "receiver_url": "http://127.0.0.1:8765",
        "receiver": {
            "listen_host": "0.0.0.0",
            "port": 8765,
            "auto_open_links": True,
        },
        "inbox_dir": inbox_dir,
        "drop_dir": drop_dir,
        "link_log_path": f"{inbox_dir}/link-log.jsonl",
        "state_dir": "~/.local/state/unixdrop",
        "drop": {
            "delete_after_send": False,
            "max_file_mb": 500,
        },
        "clipboard": {
            "mode": "off",
            "max_chars": 20000,
        },
        "tabs": {
            "default_browser": "auto",
            "firefox_debug_url": "http://127.0.0.1:9222",
        },
        "deskflow": {
            "role": "off",
            "server_start_script": "~/.config/deskflow/start-deskflow-server.sh",
            "client_start_script": "~/.config/deskflow/start-deskflow-client.sh",
        },
        "obsidian": {
            "enabled": False,
            "local_vault": "~/Obsidian/MainVault",
            "remote_vault": "",
            "conflict_strategy": "copy",
        },
    }


def _ensure_runtime_dirs(cfg) -> tuple[bool, str]:
    try:
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        cfg.drop_dir.mkdir(parents=True, exist_ok=True)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.link_log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"failed to create runtime directories: {exc}"
    return True, f"runtime directories ready: inbox={cfg.inbox_dir}, drop={cfg.drop_dir}"


def _bootstrap_tui_config() -> tuple[bool, str]:
    config_path = _active_config_path()
    created = False
    if not config_path.exists():
        try:
            _write_config_payload(_default_tui_config_payload())
            created = True
        except Exception as exc:
            return False, f"failed to create unixdrop config at {config_path}: {exc}"
    try:
        cfg = load_config()
    except Exception as exc:
        return False, f"config load failed: {exc}"
    dirs_ok, dirs_detail = _ensure_runtime_dirs(cfg)
    if not dirs_ok:
        return False, dirs_detail
    if created:
        return True, f"created starter config at {config_path}; {dirs_detail}"
    return True, dirs_detail


def _fallback_state_dir() -> Path:
    try:
        return load_config().state_dir
    except Exception:
        return Path("~/.local/state/unixdrop").expanduser()


def _process_log_path(name: str) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name)
    state_dir = _fallback_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{safe_name}.log"


def _fallback_process_log_path(name: str) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name)
    tmp_root = Path(os.environ.get("TMPDIR", "/tmp")) / "deskbridge-logs"
    tmp_root.mkdir(parents=True, exist_ok=True)
    return tmp_root / f"{safe_name}.log"


def _tail_file(path: Path, max_chars: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    output = "\n".join(lines[-12:])
    if len(output) > max_chars:
        output = output[-max_chars:]
    return output


def _deskflow_log_candidates(role: str) -> list[Path]:
    candidates = [
        _fallback_state_dir() / f"deskflow-{role}.log",
        Path.home() / "Library" / "Logs" / f"deskflow-{role}.log",
    ]
    return [path for path in candidates if path.exists()]


def _deskflow_recent_problem(role: str) -> str | None:
    for log_path in _deskflow_log_candidates(role):
        tail = _tail_file(log_path, max_chars=5000)
        for line in reversed(tail.splitlines()):
            if DESKFLOW_LOG_PROBLEM_RE.search(line):
                detail = f"{line} (log: {log_path})"
                if role == "server":
                    peer_name = _saved_deskflow_peer_name()
                    if peer_name:
                        detail = f"{detail}; expected client screen name: {peer_name}"
                return detail
    return None


def _tcp_listen_detected(port: int) -> bool:
    if _local_tcp_open("127.0.0.1", port):
        return True
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _deskflow_runtime_check(status: dict[str, str]) -> tuple[bool, str, str] | None:
    enabled = status.get("deskflow_enabled", "no").startswith("yes")
    role = status.get("deskflow_role", "off")
    if not enabled or role == "off":
        return None

    if role == "server" and not _tcp_listen_detected(24800):
        return False, "Deskflow runtime", "server role is configured but TCP 24800 is not listening"

    problem = _deskflow_recent_problem(role)
    if problem:
        return False, "Deskflow runtime", problem

    if role == "server":
        return True, "Deskflow runtime", "server listening on TCP 24800"
    return True, "Deskflow runtime", "no recent Deskflow client errors found"


def _spawn_logged_process(
    command: list[str],
    log_name: str,
    *,
    start_new_session: bool = False,
) -> tuple[subprocess.Popen, Path]:
    log_path = _process_log_path(log_name)
    try:
        handle = log_path.open("a", encoding="utf-8")
    except OSError:
        log_path = _fallback_process_log_path(log_name)
        handle = log_path.open("a", encoding="utf-8")
    with handle:
        handle.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] $ {' '.join(command)}\n")
        handle.flush()
        proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, start_new_session=start_new_session)
    return proc, log_path


def _exit_detail_with_log(prefix: str, return_code: int, log_path: Path) -> str:
    detail = f"{prefix} exited with code {return_code}; log: {log_path}"
    tail = _tail_file(log_path)
    if tail:
        detail = f"{detail}; recent output: {tail}"
    return detail


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


def _apply_deskflow_endpoints(
    *,
    role: str,
    server_hosts: str = "",
    client_name: str = "",
) -> tuple[bool, str]:
    if role == "server":
        screen_name = client_name.strip() or _saved_deskflow_peer_name()
        if not screen_name:
            return False, "client screen name is required for Deskflow server setup"
        command = [
            "--role",
            "server",
            "--client-name",
            screen_name,
            "--direction",
            "right",
        ]
        ok, detail = _run_deskflow_setup(command)
        if ok:
            return True, f"saved server screen for client: {screen_name}"
        return False, f"failed to save server setup: {detail}"

    if role == "client":
        return _apply_client_server_hosts(server_hosts)

    return False, f"unsupported Deskflow role: {role}"


def _update_quick_setup_config(role: str, peer_name: str = "") -> tuple[bool, str]:
    raw, error = _read_config_payload()
    if error:
        return False, error
    try:
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
        _write_config_payload(raw)
    except Exception as exc:
        return False, f"failed to update unixdrop config: {exc}"
    return True, f"saved {role} role and enabled two-way clipboard"


def _saved_deskflow_peer_name() -> str:
    raw, error = _read_config_payload()
    if error:
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


def _configure_startup_deskflow(
    *,
    peer_receiver: str = "",
    server_host: str = "",
    client_name: str = "",
    direction: str = "right",
) -> tuple[bool, str]:
    if sys.platform == "darwin":
        role = "server"
        screen_name = client_name.strip() or _saved_deskflow_peer_name() or "peer-laptop"
        command = [
            "--role",
            role,
            "--client-name",
            screen_name,
            "--direction",
            direction,
        ]
        receiver_input = peer_receiver.strip()
        receiver_label = "unchanged"
    elif sys.platform.startswith("linux"):
        role = "client"
        raw_server = server_host.strip() or peer_receiver.strip()
        server_endpoint = _deskflow_endpoint_from_host(raw_server)
        if not server_endpoint:
            return False, "Deskflow server IP/host is required on the client machine"
        command = [
            "--role",
            role,
            "--server-hosts",
            server_endpoint,
            "--client-name",
            _default_client_name(),
        ]
        receiver_input = peer_receiver.strip() or raw_server
        receiver_label = server_endpoint
    else:
        return False, f"automatic startup setup is unsupported on {sys.platform}"

    receiver_url = _receiver_url_from_host(receiver_input)
    if receiver_url:
        recv_ok, recv_detail = _sync_receiver_endpoint("", receiver_url)
    else:
        recv_ok, recv_detail = True, "receiver endpoint unchanged"
    if not recv_ok:
        return False, recv_detail

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
        role, screen_name if role == "server" else ""
    )
    if not config_ok:
        return False, config_detail
    started, start_detail = _start_deskflow_now()
    if not started:
        return False, f"{config_detail}; {start_detail}"
    if role == "server":
        return True, f"server ready for {screen_name}; {recv_detail}; {start_detail}"
    return True, f"client ready for {receiver_label}; {recv_detail}; {start_detail}"


def _receiver_host_is_placeholder(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    return normalized in {"", "127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


def _configured_peer_receiver_host() -> str:
    candidates: list[str] = []
    try:
        cfg = load_config()
        parsed = urlparse(cfg.receiver_url)
        if parsed.hostname:
            candidates.append(parsed.hostname)
    except Exception:
        pass

    raw, error = _read_config_payload()
    if not error:
        receiver = raw.get("receiver") if isinstance(raw.get("receiver"), dict) else {}
        host = str(receiver.get("host", "")).strip()
        if host:
            candidates.append(host)

    for candidate in candidates:
        if not _receiver_host_is_placeholder(candidate):
            return candidate
    return candidates[0] if candidates else ""


def _startup_setup_needed() -> tuple[bool, str]:
    expected_role = _default_deskflow_role_for_platform()
    if expected_role is None:
        return False, f"automatic startup setup is unsupported on {sys.platform}"

    try:
        cfg = load_config()
    except Exception as exc:
        return True, f"config needs setup: {exc}"

    missing: list[str] = []
    if cfg.deskflow_role != expected_role or not cfg.deskflow_enabled:
        missing.append(f"Deskflow {expected_role} role")

    script = _deskflow_script_for_role(cfg, expected_role)
    if not script.exists():
        missing.append(f"Deskflow {expected_role} start script")
    elif not os.access(script, os.X_OK):
        missing.append(f"executable Deskflow {expected_role} start script")

    peer_receiver = _configured_peer_receiver_host()
    if _receiver_host_is_placeholder(peer_receiver):
        missing.append("peer UnixDrop IP/host")

    if expected_role == "server" and not _saved_deskflow_peer_name():
        missing.append("Deskflow client screen name")

    if missing:
        return True, "startup setup needed: " + ", ".join(missing)
    return False, "startup setup already complete"


def _prompt_line_default(prompt: str, default: str = "") -> str:
    clean_default = default.strip()
    suffix = f" [{clean_default}]" if clean_default else ""
    entered = _prompt_line_cooked(f"{prompt}{suffix}: ").strip()
    return entered or clean_default


def _startup_deskflow_prompt(reason: str = "") -> tuple[bool, str]:
    if not sys.stdin.isatty():
        return True, "startup setup skipped (non-interactive terminal)"
    _clear()
    print(_cyan("Deskbridge startup setup"))
    if reason:
        print(reason)
    print("Enter the peer addresses once. Saved values will be reused on later starts.")
    print("")
    peer_default = _configured_peer_receiver_host()
    if sys.platform == "darwin":
        peer_receiver = _prompt_line_default("Peer UnixDrop IP/host (Linux)", peer_default)
        client_name = _prompt_line_default(
            "Deskflow client screen name (Linux hostname)",
            _saved_deskflow_peer_name() or "peer-laptop",
        )
        return _configure_startup_deskflow(peer_receiver=peer_receiver, client_name=client_name)
    if sys.platform.startswith("linux"):
        server_host = _prompt_line_default("Deskflow server IP/host (Mac)", peer_default)
        peer_receiver = _prompt_line_default("Peer UnixDrop IP/host", server_host)
        return _configure_startup_deskflow(peer_receiver=peer_receiver, server_host=server_host)
    return False, f"automatic startup setup is unsupported on {sys.platform}"


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


def _host_port_endpoint(value: str, default_port: int) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        host, port = _parse_receiver_override(text)
    except (TypeError, ValueError):
        return text
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port or default_port}"


def _receiver_url_from_host(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        host, _port = _parse_receiver_override(text)
    except (TypeError, ValueError):
        host = text
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:8765"


def _receiver_url_from_override(value: str) -> str:
    endpoint = _host_port_endpoint(value, 8765)
    if not endpoint:
        return ""
    return f"http://{endpoint}"


def _deskflow_endpoint_from_host(value: str) -> str:
    return _host_port_endpoint(value, 24800)


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

    raw, error = _read_config_payload()
    if error:
        return False, error

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
        _write_config_payload(raw)
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
        problem = _deskflow_recent_problem(running_role)
        if problem:
            return False, f"deskflow {running_role} is running but recent logs show a transfer failure: {problem}"
        if role_was_off:
            role_ok, role_detail = _set_deskflow_role(role)
            if not role_ok:
                return False, role_detail
        return True, f"deskflow {running_role} already running; no action needed"
    if not script.exists():
        return False, f"deskflow {role} start script missing: {script}"
    if not os.access(script, os.X_OK):
        return False, f"deskflow {role} start script not executable: {script}"
    log_name = f"deskflow-{role}"
    try:
        proc, log_path = _spawn_logged_process([str(script)], log_name)
        time.sleep(0.4)
        return_code = proc.poll()
        if return_code is not None:
            if sys.platform == "darwin":
                detail = _exit_detail_with_log("deskflow", return_code, log_path)
                return False, (
                    f"{detail}; allow Deskflow in "
                    "System Settings > Privacy & Security > Accessibility"
                )
            return False, _exit_detail_with_log("deskflow", return_code, log_path)
        if role_was_off:
            role_ok, role_detail = _set_deskflow_role(role)
            if not role_ok:
                return False, role_detail
            return True, f"deskflow {role} start requested (pid={proc.pid}; log={log_path}); {role_detail}"
        return True, f"deskflow {role} start requested (pid={proc.pid}; log={log_path})"
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


def _receiver_probe_host(listen_host: str) -> str:
    host = str(listen_host or "").strip()
    if host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _wait_for_receiver_start(
    proc: subprocess.Popen,
    host: str,
    port: int,
    timeout_seconds: float = 2.0,
    log_path: Path | None = None,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _local_tcp_open(host, port):
            detail = f"local receiver listening on {host}:{port} (pid={proc.pid})"
            if log_path is not None:
                detail = f"{detail}; log={log_path}"
            return True, detail
        return_code = proc.poll()
        if return_code is not None:
            if log_path is not None:
                return False, _exit_detail_with_log("local receiver", return_code, log_path)
            return False, f"local receiver exited with code {return_code}"
        time.sleep(0.1)
    detail = f"local receiver start requested but {host}:{port} did not become reachable"
    if log_path is not None:
        tail = _tail_file(log_path)
        detail = f"{detail}; log: {log_path}"
        if tail:
            detail = f"{detail}; recent output: {tail}"
    return False, detail


def _start_local_receiver_now() -> tuple[bool, str]:
    try:
        cfg = load_config()
    except Exception as exc:
        return False, f"local receiver not started because config could not be loaded: {exc}"
    receiver_port = int(cfg.port)
    probe_host = _receiver_probe_host(getattr(cfg, "listen_host", "0.0.0.0"))
    if _local_tcp_open(probe_host, receiver_port):
        return True, f"local receiver already listening on {probe_host}:{receiver_port}"

    try:
        proc, log_path = _spawn_logged_process(
            [sys.executable, "-m", "unixdrop.linux_service"],
            "local-receiver",
            start_new_session=True,
        )
    except Exception as exc:
        return False, f"local receiver start failed: {exc}"
    return _wait_for_receiver_start(proc, probe_host, receiver_port, log_path=log_path)


def _start_linux_receiver_now() -> tuple[bool, str]:
    return _start_local_receiver_now()


def _restart_deskflow_client_now() -> tuple[bool, str]:
    return _restart_deskflow_role_now("client")


def _restart_deskflow_role_now(role: str) -> tuple[bool, str]:
    if role not in {"server", "client"}:
        return False, f"unsupported Deskflow role: {role}"

    try:
        script = _configured_deskflow_script_for_role(role)
    except Exception as exc:
        return False, f"failed to load Deskflow config: {exc}"
    if not script.exists():
        return False, f"deskflow {role} start script missing: {script}"
    if not os.access(script, os.X_OK):
        return False, f"deskflow {role} start script not executable: {script}"

    stopped, stop_detail = _stop_deskflow_processes()
    if not stopped:
        return False, stop_detail

    try:
        proc, log_path = _spawn_logged_process([str(script)], f"deskflow-{role}")
        time.sleep(0.4)
        return_code = proc.poll()
    except Exception as exc:
        return False, f"deskflow {role} restart failed: {exc}"

    if return_code is not None:
        return False, _exit_detail_with_log(f"deskflow {role}", return_code, log_path)
    return True, f"deskflow {role} restart requested (pid={proc.pid}; log={log_path}; {stop_detail})"


def _deskflow_role_running(role: str) -> bool:
    patterns = {
        "server": ("deskflow-server", "deskflow-core.*server"),
        "client": ("deskflow-client", "deskflow-core.*client"),
    }
    for pattern in patterns[role]:
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
        if hasattr(cfg, "deskflow_server_start_script"):
            return cfg.deskflow_server_start_script
        return cfg.deskflow_mac_start_script
    if hasattr(cfg, "deskflow_client_start_script"):
        return cfg.deskflow_client_start_script
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
        "deskflow-core",
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
        proc, log_path = _spawn_logged_process([str(script)], f"deskflow-{next_role}")
        return True, f"deskflow switched {current_role} -> {next_role} (pid={proc.pid}; log={log_path})"
    except Exception as exc:
        return False, f"deskflow role switch failed: {exc}"


def _render(
    snapshot_time: str,
    checks: list[tuple[bool, str, str]],
    status: dict[str, str],
    latency_samples: list[float],
    interval: float,
    message: str,
    events: list[str] | None = None,
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
    if events:
        print("")
        print(_dim("Recent events"))
        width = max(_terminal_width() - 4, 40)
        for event in events[-8:]:
            print(f"  {_truncate_middle(event, width)}")


def _collect_tui_snapshot(events: deque[str]) -> tuple[list[tuple[bool, str, str]], dict[str, str]]:
    try:
        health_source = health_lines()
        checks = _parse_health(health_source)
    except Exception as exc:
        detail = f"health collection failed: {exc}"
        _record_event(events, "error", detail)
        checks = [(False, "TUI health collection", str(exc))]

    try:
        status_source = status_lines()
        status = _collect_status_map(status_source)
    except Exception as exc:
        detail = f"status collection failed: {exc}"
        _record_event(events, "error", detail)
        status = {
            "peer receiver reachable": "unknown",
            "peer receiver latency": "unknown",
            "clipboard_mode": "unknown",
            "deskflow_enabled": "no",
            "deskflow_role": "off",
            "peer hostname": "unknown",
            "local drop folder": "unknown",
            "local inbox": "unknown",
            "pending files in drop folder": "unknown",
            "last upload result": f"status collection failed: {exc}",
        }

    deskflow_check = _deskflow_runtime_check(status)
    if deskflow_check:
        checks.append(deskflow_check)
        if not deskflow_check[0]:
            _record_event(events, "error", f"{deskflow_check[1]}: {deskflow_check[2]}")

    return checks, status


def run_tui(interval_seconds: float = 3.0, once: bool = False) -> int:
    interval = max(interval_seconds, 0.5)
    message = "ready"
    latency_samples = deque(maxlen=8)
    events = deque(maxlen=10)
    bootstrap_ok, bootstrap_detail = _bootstrap_tui_config()
    message = bootstrap_detail if bootstrap_ok else f"error: {bootstrap_detail}"
    _record_event(events, "info" if bootstrap_ok else "error", bootstrap_detail)
    receiver_ok, receiver_detail = _start_local_receiver_now()
    _record_event(events, "info" if receiver_ok else "warn", receiver_detail)
    if bootstrap_ok:
        message = receiver_detail if receiver_ok else f"warning: {receiver_detail}"
    if not once and sys.stdin.isatty():
        setup_needed, setup_detail = _startup_setup_needed()
        if setup_needed:
            _record_event(events, "warn", setup_detail)
            setup_ok, setup_detail = _startup_deskflow_prompt(setup_detail)
            message = setup_detail if setup_ok else f"error: {setup_detail}"
            _record_event(events, "info" if setup_ok else "error", setup_detail)
        else:
            message = f"{message}; {setup_detail}"
            _record_event(events, "info", setup_detail)
    with _raw_stdin():
        while True:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            health, status = _collect_tui_snapshot(events)
            latency_ms = _parse_latency_ms(status.get("peer receiver latency", "unknown"))
            if latency_ms is not None:
                latency_samples.append(latency_ms)
            _render(now, health, status, list(latency_samples), interval, message, list(events))
            if once:
                return 0
            key = _read_key(interval)
            if key and key.lower() == "q":
                return 0
            if not key:
                continue
            if key.lower() == "e":
                deskflow_role = status.get("deskflow_role", "off")
                if deskflow_role == "off":
                    default_role = _default_deskflow_role_for_platform()
                    deskflow_role = default_role or "off"
                if deskflow_role == "server":
                    entered = ""
                    client_name = _prompt_line("Deskflow client screen name: ").strip()
                    prefix = "saved server setup"
                    endpoint_label = client_name or _saved_deskflow_peer_name() or "client screen"
                else:
                    entered = _prompt_line(
                        "Server endpoints (blank=LAN discovery, or lan:24800,tailnet:24800): "
                    ).strip()
                    client_name = ""
                    prefix = "using LAN discovery" if not entered else "saved endpoints"
                    endpoint_label = entered or "LAN discovery"
                receiver_input = _prompt_line("Peer UnixDrop IP/host (blank=same as first server host): ").strip()
                receiver_override = None
                if receiver_input:
                    receiver_override = _receiver_url_from_override(receiver_input)
                ok, detail = _apply_deskflow_endpoints(
                    role=deskflow_role,
                    server_hosts=entered,
                    client_name=client_name,
                )
                recv_ok, recv_detail = _sync_receiver_endpoint(entered, receiver_override)
                message = detail
                if ok:
                    _, start_detail = _restart_deskflow_role_now(deskflow_role)
                    message = f"{prefix}: {endpoint_label} | {recv_detail} | {start_detail}"
                    if not recv_ok:
                        message = f"{prefix}: {endpoint_label} | warning: {recv_detail} | {start_detail}"
                else:
                    message = f"{detail} | {recv_detail}"
                    if not recv_ok:
                        message = f"{detail} | warning: {recv_detail}"
                _record_event(events, "info" if ok and recv_ok else "warn", message)
                continue
            if key.lower() == "s":
                ok, detail = _quick_setup_deskflow(status.get("peer hostname", ""))
                message = detail if ok else f"error: {detail}"
                _record_event(events, "info" if ok else "error", detail)
                continue
            if key.lower() == "x":
                ok, detail = _stop_all_now()
                _record_event(events, "info" if ok else "error", detail)
                _clear()
                print(_green(detail) if ok else _red(f"Shutdown incomplete: {detail}"))
                return 0 if ok else 1
            if key.lower() == "d":
                ok, detail = _start_deskflow_now()
                message = detail if ok else f"error: {detail}"
                _record_event(events, "info" if ok else "error", detail)
                continue
            if key.lower() == "r":
                ok, detail = _swap_deskflow_role_now()
                message = detail if ok else f"error: {detail}"
                _record_event(events, "info" if ok else "error", detail)
                continue
            if key.lower() == "o":
                ok, detail = _open_drop_folder_now()
                message = detail if ok else f"error: {detail}"
                _record_event(events, "info" if ok else "error", detail)
