from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import socket
import subprocess
import sys
from pathlib import Path

from unixdrop.platform_tools import find_deskflow_binary, resolve_deskflow_command


def _log(message: str) -> None:
    print(f"[deskflow setup] {message}")


def _die(message: str) -> None:
    raise SystemExit(f"[deskflow setup] error: {message}")


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    _die(f"unsupported platform: {sys.platform}")
    return ""


def _quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def _hostname() -> str:
    return socket.gethostname() or "unixdrop"


def _opposite_direction(direction: str) -> str:
    return {
        "right": "left",
        "left": "right",
        "up": "down",
        "down": "up",
    }[direction]


def _find_deskflow_binary(platform: str, binary_name: str) -> str | None:
    return find_deskflow_binary(platform, binary_name)


def _resolve_deskflow_command(platform: str, role: str) -> tuple[str, str] | None:
    return resolve_deskflow_command(role, platform)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_server_config(path: Path, server_name: str, client_name: str, direction: str) -> None:
    reverse = _opposite_direction(direction)
    path.write_text(
        f"""section: screens
    {server_name}:
    {client_name}:
end

section: links
    {server_name}:
        {direction} = {client_name}
    {client_name}:
        {reverse} = {server_name}
end

section: options
    relativeMouseMoves = true
end
""",
        encoding="utf-8",
    )


def _write_client_settings(path: Path, client_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"[core]\ncomputerName={client_name}\n", encoding="utf-8")
        return

    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    inserted = False
    in_core = False
    saw_core = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[core]":
            saw_core = True
            in_core = True
            output.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_core and not inserted:
                output.append(f"computerName={client_name}")
                inserted = True
            in_core = False
        if in_core and stripped.startswith("computerName"):
            if not inserted:
                output.append(f"computerName={client_name}")
                inserted = True
            continue
        output.append(line)

    if saw_core and in_core and not inserted:
        output.append(f"computerName={client_name}")
        inserted = True
    if not inserted:
        if output and output[-1].strip():
            output.append("")
        output.extend(["[core]", f"computerName={client_name}"])
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _install_linux_autostart(role: str, start_script: Path) -> None:
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_file = service_dir / f"deskflow-{role}.service"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file.write_text(
        f"""[Unit]
Description=Deskflow {role}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={start_script}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
""",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"deskflow-{role}.service"], check=True)
    _log(f"enabled systemd user service: deskflow-{role}.service")


def _install_macos_autostart(role: str, start_script: Path) -> None:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_file = plist_dir / f"com.unixdrop.deskflow.{role}.plist"
    plist_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": f"com.unixdrop.deskflow.{role}",
        "ProgramArguments": [str(start_script)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / f"deskflow-{role}.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / f"deskflow-{role}.log"),
    }
    with plist_file.open("wb") as handle:
        plistlib.dump(payload, handle)
    subprocess.run(["launchctl", "unload", str(plist_file)], check=False)
    subprocess.run(["launchctl", "load", str(plist_file)], check=True)
    _log(f"loaded launch agent: {plist_file}")


def _system_autostart(platform: str, role: str, start_script: Path) -> None:
    if platform == "linux":
        _install_linux_autostart(role, start_script)
    else:
        _install_macos_autostart(role, start_script)


def _write_server(platform: str, args: argparse.Namespace, command: tuple[str, str], config_dir: Path) -> None:
    if not args.client_name:
        _die("--client-name is required for server role")

    server_name = args.server_name or _hostname()
    binary, mode = command
    server_config = config_dir / "deskflow-server.conf"
    settings_file = config_dir / "deskflow-core-server-settings.conf"
    start_script = config_dir / "start-deskflow-server.sh"

    _write_server_config(server_config, server_name, args.client_name, args.direction)
    settings_file.write_text(
        f"""[core]
coreMode=2
computerName={server_name}

[server]
externalConfig=true
externalConfigFile={server_config}

[security]
tlsEnabled=false
checkPeerFingerprints=false
""",
        encoding="utf-8",
    )

    launch_command = (
        f"{_quote(binary)} server -s {_quote(settings_file)} &"
        if mode == "server"
        else f"{_quote(binary)} --no-daemon --name {_quote(server_name)} --config {_quote(server_config)} &"
    )
    body = f"""#!/usr/bin/env bash
set -euo pipefail
if command -v lsof >/dev/null 2>&1; then
  existing_listener="$(lsof -nP -iTCP:24800 -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${{existing_listener}}" ]]; then
    if printf '%s\n' "${{existing_listener}}" | grep -Eiq 'deskflow|barrier|synergy'; then
      echo "Deskflow server already listening on TCP 24800; skipping duplicate start."
      exit 0
    fi
    echo "TCP 24800 already in use by another process. Resolve it before starting Deskflow server." >&2
    exit 1
  fi
fi
{_quote(sys.executable)} -m unixdrop.discovery serve --name {_quote(server_name)} --service-port 24800 &
discovery_pid=$!
trap 'kill "${{discovery_pid}}" >/dev/null 2>&1 || true' EXIT INT TERM
{launch_command}
deskflow_pid=$!
wait "${{deskflow_pid}}"
"""
    _write_executable(start_script, body)

    _log(f"server config written: {server_config}")
    _log(f"start command: {start_script}")
    if args.autostart:
        _system_autostart(platform, "server", start_script)

    hint = " --autostart" if args.autostart else ""
    print(
        "\nNext steps (server):\n"
        f"1) Start now: {start_script}\n"
        f"2) On client machine run:\n   deskbridge deskflow --role client{hint}\n"
        "3) Ensure firewall allows TCP 24800 and UDP 24801 on the server."
    )


def _client_script_body(
    *,
    server_ip: str,
    config_dir: Path,
    settings_file: Path,
    client_name: str,
    command: tuple[str, str],
) -> str:
    binary, mode = command
    if mode == "client":
        run_line = f"{_quote(binary)} client -s {_quote(settings_file)}"
    else:
        run_line = f"{_quote(binary)} --no-daemon --name {_quote(client_name)} \"${{selected_server}}\""

    return f"""#!/usr/bin/env bash
set -euo pipefail
server_candidates_csv={_quote(server_ip)}
if [[ -z "${{server_candidates_csv}}" ]]; then
  server_candidates_csv="$({_quote(sys.executable)} -m unixdrop.discovery discover --cache {_quote(config_dir / "discovered-server.json")} --timeout 4)" || {{
    echo "Could not discover the Deskflow server. Check that both machines are on the same LAN and UDP 24801 is allowed." >&2
    exit 1
  }}
  echo "Discovered Deskflow server: ${{server_candidates_csv}}"
fi

split_server_endpoint_runtime() {{
  local value="$1"
  local host="$value"
  local port="24800"
  if [[ "$value" == *:* ]]; then
    host="${{value%:*}}"
    port="${{value##*:}}"
  fi
  printf '%s %s\n' "${{host}}" "${{port}}"
}}

tcp_reachable_runtime() {{
  local host="$1"
  local port="$2"
  if command -v nc >/dev/null 2>&1; then
    nc -z -w 2 "${{host}}" "${{port}}" >/dev/null 2>&1
    return $?
  fi
  {_quote(sys.executable)} - "$host" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=2):
        pass
except OSError:
    raise SystemExit(1)
PY
}}

first_reachable_endpoint_runtime() {{
  local endpoints_csv="$1"
  local raw=""
  local endpoint=""
  local host=""
  local port=""
  IFS=',' read -r -a endpoints <<< "${{endpoints_csv}}"
  for raw in "${{endpoints[@]}}"; do
    endpoint="${{raw#"${{raw%%[![:space:]]*}}"}}"
    endpoint="${{endpoint%"${{endpoint##*[![:space:]]}}"}}"
    [[ -n "${{endpoint}}" ]] || continue
    read -r host port <<<"$(split_server_endpoint_runtime "${{endpoint}}")"
    if tcp_reachable_runtime "${{host}}" "${{port}}"; then
      printf '%s\n' "${{endpoint}}"
      return 0
    fi
  done
  return 1
}}

update_remote_host_runtime() {{
  local endpoint="$1"
  local host=""
  local port=""
  local endpoint_with_port=""
  read -r host port <<<"$(split_server_endpoint_runtime "${{endpoint}}")"
  [[ -n "${{host}}" ]] || return 0
  endpoint_with_port="${{host}}:${{port}}"
  cat > {_quote(settings_file)} <<SETTINGS
[core]
coreMode=1
computerName={client_name}
port=${{port}}

[client]
remoteHost=${{endpoint_with_port}}

[security]
tlsEnabled=false
checkPeerFingerprints=false
SETTINGS
}}

DESKBRIDGE_CLIENT_LOCK_DIR=""

cleanup_client_lock_runtime() {{
  if [[ -n "${{DESKBRIDGE_CLIENT_LOCK_DIR:-}}" ]]; then
    rm -rf "${{DESKBRIDGE_CLIENT_LOCK_DIR}}" >/dev/null 2>&1 || true
  fi
}}

pid_is_deskflow_runtime() {{
  local pid="$1"
  local command_line=""
  [[ -n "${{pid}}" ]] || return 1
  kill -0 "${{pid}}" >/dev/null 2>&1 || return 1
  if command -v ps >/dev/null 2>&1; then
    command_line="$(ps -p "${{pid}}" -o command= 2>/dev/null || true)"
    printf '%s\n' "${{command_line}}" | grep -Eiq 'deskflow|barrier|synergy'
    return $?
  fi
  return 0
}}

guard_single_client_instance_runtime() {{
  local lock_token={_quote("".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in client_name))}
  local lock_dir="${{TMPDIR:-/tmp}}/deskbridge-deskflow-client-${{lock_token}}.lock"
  local lock_pid_file="${{lock_dir}}/pid"
  local existing_pid=""
  if mkdir "${{lock_dir}}" >/dev/null 2>&1; then
    DESKBRIDGE_CLIENT_LOCK_DIR="${{lock_dir}}"
    printf '%s\n' "$$" > "${{lock_pid_file}}"
    trap cleanup_client_lock_runtime EXIT INT TERM
    return 0
  fi
  if [[ -f "${{lock_pid_file}}" ]]; then
    existing_pid="$(cat "${{lock_pid_file}}" 2>/dev/null || true)"
    if pid_is_deskflow_runtime "${{existing_pid}}"; then
      echo "Deskflow client already running for {client_name} (pid=${{existing_pid}}); skipping duplicate start."
      exit 0
    fi
  fi
  rm -rf "${{lock_dir}}" >/dev/null 2>&1 || true
  if mkdir "${{lock_dir}}" >/dev/null 2>&1; then
    DESKBRIDGE_CLIENT_LOCK_DIR="${{lock_dir}}"
    printf '%s\n' "$$" > "${{lock_pid_file}}"
    trap cleanup_client_lock_runtime EXIT INT TERM
    return 0
  fi
  echo "Could not acquire Deskflow client start lock: ${{lock_dir}}" >&2
  exit 1
}}

guard_single_client_instance_runtime
selected_server="$(first_reachable_endpoint_runtime "${{server_candidates_csv}}" || true)"
if [[ -z "${{selected_server}}" ]]; then
  echo "No Deskflow server is accepting TCP connections from: ${{server_candidates_csv}}" >&2
  echo "Start the server script on the keyboard/mouse machine and allow TCP 24800 through the firewall." >&2
  exit 1
fi
update_remote_host_runtime "${{selected_server}}"
{run_line}
"""


def _write_client(platform: str, args: argparse.Namespace, command: tuple[str, str], config_dir: Path) -> None:
    server_ip = args.server_hosts or args.server_ip or ""
    client_name = args.client_name or _hostname()
    start_script = config_dir / "start-deskflow-client.sh"
    settings_file = config_dir / "deskflow-core-client-settings.conf"

    for path in (
        config_dir / "Deskflow.conf",
        Path.home() / ".config" / "Deskflow" / "Deskflow.conf",
        *((Path.home() / "Library" / "Deskflow" / "Deskflow.conf",) if platform == "macos" else ()),
    ):
        _write_client_settings(path, client_name)
        _log(f"client screen name written: {path}")

    _write_executable(
        start_script,
        _client_script_body(
            server_ip=server_ip,
            config_dir=config_dir,
            settings_file=settings_file,
            client_name=client_name,
            command=command,
        ),
    )
    _log(f"client launcher written: {start_script}")
    _log(f"start command: {start_script}")
    if args.autostart:
        _system_autostart(platform, "client", start_script)
    print(
        "\nNext steps (client):\n"
        f"1) Start now: {start_script}\n"
        "2) The server is discovered automatically at every start; the last result is cached as a fallback."
    )


def _verify(platform: str, args: argparse.Namespace, command: tuple[str, str] | None, config_dir: Path) -> int:
    failures = 0

    def check(ok: bool, name: str, detail: str = "") -> None:
        nonlocal failures
        print(f"[{'ok' if ok else 'fail'}] {name}{': ' + detail if detail else ''}")
        if not ok:
            failures += 1

    check(command is not None, f"deskflow {args.role} command found", command[0] if command else "not found")
    start_script = config_dir / f"start-deskflow-{args.role}.sh"
    check(start_script.exists() and os.access(start_script, os.X_OK), f"{args.role} start script executable", str(start_script))
    if args.role == "server":
        check((config_dir / "deskflow-server.conf").exists(), "server config exists", str(config_dir / "deskflow-server.conf"))

    if platform == "linux":
        service_file = Path.home() / ".config" / "systemd" / "user" / f"deskflow-{args.role}.service"
        check(service_file.exists(), "systemd user service file exists", str(service_file))
    else:
        plist_file = Path.home() / "Library" / "LaunchAgents" / f"com.unixdrop.deskflow.{args.role}.plist"
        check(plist_file.exists(), "launch agent file exists", str(plist_file))

    if failures:
        _die(f"verification failed with {failures} issue(s)")
    _log("verification passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure Deskflow keyboard/mouse sharing")
    parser.add_argument("--role", choices=["server", "client"], required=True)
    parser.add_argument("--server-ip")
    parser.add_argument("--server-hosts")
    parser.add_argument("--client-name")
    parser.add_argument("--server-name")
    parser.add_argument("--direction", choices=["right", "left", "up", "down"], default="right")
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--config-dir", default="~/.config/deskflow")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    platform = _platform()
    config_dir = Path(args.config_dir).expanduser()
    command = _resolve_deskflow_command(platform, args.role)

    if args.verify:
        return _verify(platform, args, command, config_dir)
    if command is None:
        _die(f"deskflow {args.role} command not found. Install Deskflow first.")

    config_dir.mkdir(parents=True, exist_ok=True)
    if args.role == "server":
        _write_server(platform, args, command, config_dir)
    else:
        _write_client(platform, args, command, config_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
