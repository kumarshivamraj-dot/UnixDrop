#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  configure_deskflow.sh --role server [options]
  configure_deskflow.sh --role client [options]

Required:
  --role ROLE                  server | client

Server role options:
  --client-name NAME           Client screen name in Deskflow config
  --direction DIR              right | left | up | down (default: right)
  --server-name NAME           Server screen name (default: local hostname)

Client role options:
  --server-ip IP               Server endpoint (host or host:port)
  --server-hosts LIST          Comma-separated endpoints. Order matters.
  --client-name NAME           Client runtime screen name override (default: local hostname)

Shared options:
  --verify                     Verify existing Deskflow setup for this role
  --autostart                  Install and enable autostart service
  --config-dir PATH            Config directory (default: ~/.config/deskflow)
  --help                       Show this help

Examples:
  ./scripts/configure_deskflow.sh --role server --client-name thinkpad --direction right --autostart
  ./scripts/configure_deskflow.sh --role client --server-ip 192.168.1.50 --autostart
  ./scripts/configure_deskflow.sh --role client --server-hosts 192.168.1.50:24800,100.64.0.12:24800 --autostart
  ./scripts/configure_deskflow.sh --role server --verify
EOF
}

log() {
  printf '[deskflow setup] %s\n' "$*"
}

die() {
  printf '[deskflow setup] error: %s\n' "$*" >&2
  exit 1
}

opposite_direction() {
  case "$1" in
    right) echo "left" ;;
    left) echo "right" ;;
    up) echo "down" ;;
    down) echo "up" ;;
    *) die "invalid direction: $1" ;;
  esac
}

detect_platform() {
  case "$(uname -s)" in
    Linux) echo "linux" ;;
    Darwin) echo "macos" ;;
    *) die "unsupported platform: $(uname -s)" ;;
  esac
}

detect_deskflow_binary() {
  local platform="$1"
  local binary_name="$2"
  if command -v "${binary_name}" >/dev/null 2>&1; then
    command -v "${binary_name}"
    return
  fi

  if [[ "${platform}" == "macos" ]]; then
    local mac_path="/Applications/Deskflow.app/Contents/MacOS/${binary_name}"
    if [[ -x "${mac_path}" ]]; then
      echo "${mac_path}"
      return
    fi
  fi

  die "${binary_name} not found. Install Deskflow first."
}

find_deskflow_binary() {
  local platform="$1"
  local binary_name="$2"
  if command -v "${binary_name}" >/dev/null 2>&1; then
    command -v "${binary_name}"
    return 0
  fi
  if [[ "${platform}" == "macos" ]]; then
    local mac_path="/Applications/Deskflow.app/Contents/MacOS/${binary_name}"
    if [[ -x "${mac_path}" ]]; then
      echo "${mac_path}"
      return 0
    fi
  fi
  return 1
}

resolve_deskflow_command() {
  local platform="$1"
  local role="$2"
  local bin=""
  local mode=""
  if [[ "${role}" == "server" ]]; then
    bin="$(find_deskflow_binary "${platform}" "deskflow-server" || true)"
    if [[ -n "${bin}" ]]; then
      printf '%s\t%s\n' "${bin}" "${mode}"
      return 0
    fi
    bin="$(find_deskflow_binary "${platform}" "deskflow-core" || true)"
    if [[ -n "${bin}" ]]; then
      mode="server"
      printf '%s\t%s\n' "${bin}" "${mode}"
      return 0
    fi
    return 1
  fi

  bin="$(find_deskflow_binary "${platform}" "deskflow-client" || true)"
  if [[ -n "${bin}" ]]; then
    printf '%s\t%s\n' "${bin}" "${mode}"
    return 0
  fi
  bin="$(find_deskflow_binary "${platform}" "deskflow-core" || true)"
  if [[ -n "${bin}" ]]; then
    mode="client"
    printf '%s\t%s\n' "${bin}" "${mode}"
    return 0
  fi
  return 1
}

write_server_config() {
  local config_file="$1"
  local server_name="$2"
  local client_name="$3"
  local direction="$4"
  local reverse_direction="$5"

  cat > "${config_file}" <<EOF
section: screens
    ${server_name}:
    ${client_name}:
end

section: links
    ${server_name}:
        ${direction} = ${client_name}
    ${client_name}:
        ${reverse_direction} = ${server_name}
end

section: options
    relativeMouseMoves = true
end
EOF
}

write_start_script() {
  local path="$1"
  local content="$2"
  cat > "${path}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
${content}
EOF
  chmod +x "${path}"
}

sync_macos_core_server_config() {
  local server_config_file="$1"
  local fallback_dir="${HOME}/Library/Deskflow"
  local fallback_file="${fallback_dir}/deskflow-server.conf"
  mkdir -p "${fallback_dir}"
  cp "${server_config_file}" "${fallback_file}"
  log "synced macOS core fallback config: ${fallback_file}"
}

install_linux_autostart() {
  local role="$1"
  local start_script="$2"
  local service_dir="${HOME}/.config/systemd/user"
  local service_file="${service_dir}/deskflow-${role}.service"

  mkdir -p "${service_dir}"
  cat > "${service_file}" <<EOF
[Unit]
Description=Deskflow ${role}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${start_script}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now "deskflow-${role}.service"
  log "enabled systemd user service: deskflow-${role}.service"
}

install_macos_autostart() {
  local role="$1"
  local start_script="$2"
  local plist_dir="${HOME}/Library/LaunchAgents"
  local plist_file="${plist_dir}/com.unixdrop.deskflow.${role}.plist"

  mkdir -p "${plist_dir}"
  cat > "${plist_file}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.unixdrop.deskflow.${role}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${start_script}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${HOME}/Library/Logs/deskflow-${role}.log</string>
  <key>StandardErrorPath</key>
  <string>${HOME}/Library/Logs/deskflow-${role}.log</string>
</dict>
</plist>
EOF

  launchctl unload "${plist_file}" >/dev/null 2>&1 || true
  launchctl load "${plist_file}"
  log "loaded launch agent: ${plist_file}"
}

VERIFY_FAILS=0

check_result() {
  local ok="$1"
  local title="$2"
  local detail="${3:-}"
  if [[ "${ok}" == "true" ]]; then
    printf '[ok] %s' "${title}"
    [[ -n "${detail}" ]] && printf ': %s' "${detail}"
    printf '\n'
    return
  fi
  printf '[fail] %s' "${title}"
  [[ -n "${detail}" ]] && printf ': %s' "${detail}"
  printf '\n'
  VERIFY_FAILS=$((VERIFY_FAILS + 1))
}

is_port_24800_listening() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:24800 -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -ltn | grep -q ':24800'
    return $?
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -q ':24800'
    return $?
  fi
  return 2
}

tcp_reachable() {
  local host="$1"
  local port="$2"
  if command -v nc >/dev/null 2>&1; then
    nc -z -w 2 "${host}" "${port}" >/dev/null 2>&1
    return $?
  fi
  return 2
}

split_server_endpoint() {
  local value="$1"
  local host="$value"
  local port="24800"
  if [[ "${value}" == *:* ]]; then
    host="${value%:*}"
    port="${value##*:}"
  fi
  printf '%s %s\n' "${host}" "${port}"
}

first_reachable_endpoint_from_csv() {
  local endpoints_csv="$1"
  local first_endpoint=""
  local raw=""
  local endpoint=""
  local host=""
  local port=""
  IFS=',' read -r -a endpoints <<< "${endpoints_csv}"
  for raw in "${endpoints[@]}"; do
    endpoint="${raw#"${raw%%[![:space:]]*}"}"
    endpoint="${endpoint%"${endpoint##*[![:space:]]}"}"
    [[ -n "${endpoint}" ]] || continue
    if [[ -z "${first_endpoint}" ]]; then
      first_endpoint="${endpoint}"
    fi
    read -r host port <<<"$(split_server_endpoint "${endpoint}")"
    if tcp_reachable "${host}" "${port}"; then
      printf '%s\n' "${endpoint}"
      return 0
    fi
  done
  if [[ -n "${first_endpoint}" ]]; then
    printf '%s\n' "${first_endpoint}"
    return 0
  fi
  return 1
}

extract_client_server_ip() {
  local start_script="$1"
  if [[ ! -f "${start_script}" ]]; then
    return 1
  fi
  sed -n 's/.*"\([^"]*\)"[[:space:]]*$/\1/p' "${start_script}" | tail -n 1
}

verify_server_setup() {
  local platform="$1"
  local config_dir="$2"
  local deskflow_server_path="${3:-}"
  local deskflow_server_mode="${4:-}"
  local server_config_file="${config_dir}/deskflow-server.conf"
  local start_script="${config_dir}/start-deskflow-server.sh"

  if [[ -n "${deskflow_server_path}" ]]; then
    if [[ -n "${deskflow_server_mode}" ]]; then
      check_result "true" "deskflow server command found" "${deskflow_server_path} ${deskflow_server_mode}"
    else
      check_result "true" "deskflow server command found" "${deskflow_server_path}"
    fi
  else
    check_result "false" "deskflow server command found" "not found in PATH or /Applications"
  fi

  [[ -f "${server_config_file}" ]] && check_result "true" "server config exists" "${server_config_file}" || check_result "false" "server config exists" "${server_config_file}"
  [[ -x "${start_script}" ]] && check_result "true" "server start script executable" "${start_script}" || check_result "false" "server start script executable" "${start_script}"

  if [[ "${platform}" == "linux" ]]; then
    local unit_file="${HOME}/.config/systemd/user/deskflow-server.service"
    if [[ -f "${unit_file}" ]]; then
      check_result "true" "systemd user service file exists" "${unit_file}"
      if systemctl --user is-active --quiet deskflow-server.service; then
        check_result "true" "systemd service active" "deskflow-server.service"
      else
        check_result "false" "systemd service active" "deskflow-server.service is not active"
      fi
    else
      check_result "false" "systemd user service file exists" "${unit_file}"
    fi
  else
    local plist_file="${HOME}/Library/LaunchAgents/com.unixdrop.deskflow.server.plist"
    if [[ -f "${plist_file}" ]]; then
      check_result "true" "launch agent file exists" "${plist_file}"
      if launchctl list | grep -q "com.unixdrop.deskflow.server"; then
        check_result "true" "launch agent loaded" "com.unixdrop.deskflow.server"
      else
        check_result "false" "launch agent loaded" "com.unixdrop.deskflow.server is not loaded"
      fi
    else
      check_result "false" "launch agent file exists" "${plist_file}"
    fi
  fi

  if is_port_24800_listening; then
    check_result "true" "port 24800 listening" "Deskflow server appears up"
  else
    check_result "false" "port 24800 listening" "start server using ${start_script}"
  fi
}

verify_client_setup() {
  local platform="$1"
  local config_dir="$2"
  local deskflow_client_path="${3:-}"
  local deskflow_client_mode="${4:-}"
  local server_ip_input="$5"
  local start_script="${config_dir}/start-deskflow-client.sh"
  local server_ip_resolved="${server_ip_input}"

  if [[ -n "${deskflow_client_path}" ]]; then
    if [[ -n "${deskflow_client_mode}" ]]; then
      check_result "true" "deskflow client command found" "${deskflow_client_path} ${deskflow_client_mode}"
    else
      check_result "true" "deskflow client command found" "${deskflow_client_path}"
    fi
  else
    check_result "false" "deskflow client command found" "not found in PATH or /Applications"
  fi

  [[ -x "${start_script}" ]] && check_result "true" "client start script executable" "${start_script}" || check_result "false" "client start script executable" "${start_script}"

  if [[ -z "${server_ip_resolved}" ]]; then
    server_ip_resolved="$(extract_client_server_ip "${start_script}" || true)"
  fi
  if [[ -n "${server_ip_resolved}" ]]; then
    check_result "true" "server address configured" "${server_ip_resolved}"
  else
    check_result "false" "server address configured" "pass --server-ip or re-run client setup"
    server_ip_resolved=""
  fi

  if [[ -n "${server_ip_resolved}" ]]; then
    selected_endpoint="$(first_reachable_endpoint_from_csv "${server_ip_resolved}" || true)"
    if [[ -z "${selected_endpoint}" ]]; then
      check_result "false" "server endpoint parsed" "invalid endpoint list"
    else
      read -r server_host server_port <<<"$(split_server_endpoint "${selected_endpoint}")"
      if tcp_reachable "${server_host}" "${server_port}"; then
        check_result "true" "server reachable on tcp/${server_port}" "${server_host}"
      else
        check_result "false" "server reachable on tcp/${server_port}" "${server_host}"
      fi
      if [[ "${server_ip_resolved}" == *,* ]]; then
        check_result "true" "endpoint selection policy" "prefer first reachable from: ${server_ip_resolved}"
      fi
    fi
  fi

  if [[ "${platform}" == "linux" ]]; then
    local unit_file="${HOME}/.config/systemd/user/deskflow-client.service"
    [[ -f "${unit_file}" ]] && check_result "true" "systemd user service file exists" "${unit_file}" || check_result "false" "systemd user service file exists" "${unit_file}"
  else
    local plist_file="${HOME}/Library/LaunchAgents/com.unixdrop.deskflow.client.plist"
    [[ -f "${plist_file}" ]] && check_result "true" "launch agent file exists" "${plist_file}" || check_result "false" "launch agent file exists" "${plist_file}"
  fi
}

role=""
server_ip=""
server_hosts=""
server_name="$(hostname 2>/dev/null || hostname -s)"
client_name=""
direction="right"
autostart="false"
verify_mode="false"
autostart_hint=""
config_dir="${HOME}/.config/deskflow"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)
      role="${2:-}"
      shift 2
      ;;
    --server-ip)
      server_ip="${2:-}"
      shift 2
      ;;
    --server-hosts)
      server_hosts="${2:-}"
      shift 2
      ;;
    --server-name)
      server_name="${2:-}"
      shift 2
      ;;
    --client-name)
      client_name="${2:-}"
      shift 2
      ;;
    --direction)
      direction="${2:-}"
      shift 2
      ;;
    --autostart)
      autostart="true"
      shift
      ;;
    --verify)
      verify_mode="true"
      shift
      ;;
    --config-dir)
      config_dir="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ -n "${role}" ]] || die "--role is required"
[[ "${role}" == "server" || "${role}" == "client" ]] || die "--role must be server or client"
if [[ -n "${server_hosts}" ]]; then
  server_ip="${server_hosts}"
fi

case "${direction}" in
  right|left|up|down) ;;
  *) die "--direction must be right, left, up, or down" ;;
esac

platform="$(detect_platform)"
deskflow_server_bin=""
deskflow_client_bin=""
deskflow_server_mode=""
deskflow_client_mode=""
if [[ "${verify_mode}" == "true" ]]; then
  if [[ "${role}" == "server" ]]; then
    if read -r deskflow_server_bin deskflow_server_mode < <(resolve_deskflow_command "${platform}" "server"); then
      :
    fi
  else
    if read -r deskflow_client_bin deskflow_client_mode < <(resolve_deskflow_command "${platform}" "client"); then
      :
    fi
  fi
else
  if [[ "${role}" == "server" ]]; then
    if ! read -r deskflow_server_bin deskflow_server_mode < <(resolve_deskflow_command "${platform}" "server"); then
      die "deskflow server command not found. Install Deskflow first."
    fi
  else
    if ! read -r deskflow_client_bin deskflow_client_mode < <(resolve_deskflow_command "${platform}" "client"); then
      die "deskflow client command not found. Install Deskflow first."
    fi
  fi
fi

if [[ "${verify_mode}" == "true" ]]; then
  log "running verification for role=${role}"
  if [[ "${role}" == "server" ]]; then
    verify_server_setup "${platform}" "${config_dir}" "${deskflow_server_bin}" "${deskflow_server_mode}"
  else
    verify_client_setup "${platform}" "${config_dir}" "${deskflow_client_bin}" "${deskflow_client_mode}" "${server_ip}"
  fi
  if [[ "${VERIFY_FAILS}" -gt 0 ]]; then
    die "verification failed with ${VERIFY_FAILS} issue(s)"
  fi
  log "verification passed"
  exit 0
fi

mkdir -p "${config_dir}"

if [[ "${role}" == "server" ]]; then
  [[ -n "${client_name}" ]] || die "--client-name is required for server role"
  reverse_direction="$(opposite_direction "${direction}")"
  server_config_file="${config_dir}/deskflow-server.conf"
  start_script="${config_dir}/start-deskflow-server.sh"

  write_server_config "${server_config_file}" "${server_name}" "${client_name}" "${direction}" "${reverse_direction}"
  if [[ "${platform}" == "macos" && "${deskflow_server_mode}" == "server" ]]; then
    sync_macos_core_server_config "${server_config_file}"
  fi
  server_start_body="$(cat <<EOF
new_instance_flag=""
if "${deskflow_server_bin}" --help 2>&1 | grep -q -- '--new-instance'; then
  new_instance_flag="--new-instance"
fi
if command -v lsof >/dev/null 2>&1; then
  existing_listener="\$(lsof -nP -iTCP:24800 -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "\${existing_listener}" ]]; then
    if printf '%s\n' "\${existing_listener}" | grep -Eiq 'deskflow|barrier|synergy'; then
      echo "Deskflow server already listening on TCP 24800; skipping duplicate start."
      exit 0
    fi
    echo "TCP 24800 already in use by another process. Resolve it before starting Deskflow server." >&2
    exit 1
  fi
fi
exec "${deskflow_server_bin}" ${deskflow_server_mode} \${new_instance_flag} --no-daemon --name "${server_name}" --config "${server_config_file}"
EOF
)"
  if [[ -n "${deskflow_server_mode}" ]]; then
    write_start_script "${start_script}" "${server_start_body}"
  else
    write_start_script "${start_script}" "${server_start_body}"
  fi

  log "server config written: ${server_config_file}"
  log "start command: ${start_script}"

  if [[ "${autostart}" == "true" ]]; then
    if [[ "${platform}" == "linux" ]]; then
      install_linux_autostart "server" "${start_script}"
    else
      install_macos_autostart "server" "${start_script}"
    fi
  fi

  if [[ "${autostart}" == "true" ]]; then
    autostart_hint=" --autostart"
  fi

  cat <<EOF

Next steps (server):
1) Start now: ${start_script}
2) On client machine run:
   ./scripts/configure_deskflow.sh --role client --server-ip <server-ip>${autostart_hint}
3) Ensure firewall allows TCP port 24800 on server.
EOF
  exit 0
fi

[[ -n "${server_ip}" ]] || die "--server-ip is required for client role"
client_start_script="${config_dir}/start-deskflow-client.sh"
client_runtime_name="${client_name:-$(hostname 2>/dev/null || hostname -s)}"
client_start_body="$(cat <<EOF
new_instance_flag=""
if "${deskflow_client_bin}" --help 2>&1 | grep -q -- '--new-instance'; then
  new_instance_flag="--new-instance"
fi
server_candidates_csv="${server_ip}"

split_server_endpoint_runtime() {
  local value="\$1"
  local host="\$value"
  local port="24800"
  if [[ "\$value" == *:* ]]; then
    host="\${value%:*}"
    port="\${value##*:}"
  fi
  printf '%s %s\n' "\${host}" "\${port}"
}

tcp_reachable_runtime() {
  local host="\$1"
  local port="\$2"
  if command -v nc >/dev/null 2>&1; then
    nc -z -w 2 "\${host}" "\${port}" >/dev/null 2>&1
    return \$?
  fi
  return 2
}

first_reachable_endpoint_runtime() {
  local endpoints_csv="\$1"
  local first_endpoint=""
  local raw=""
  local endpoint=""
  local host=""
  local port=""
  IFS=',' read -r -a endpoints <<< "\${endpoints_csv}"
  for raw in "\${endpoints[@]}"; do
    endpoint="\${raw#"\${raw%%[![:space:]]*}"}"
    endpoint="\${endpoint%"\${endpoint##*[![:space:]]}"}"
    [[ -n "\${endpoint}" ]] || continue
    if [[ -z "\${first_endpoint}" ]]; then
      first_endpoint="\${endpoint}"
    fi
    read -r host port <<<"\$(split_server_endpoint_runtime "\${endpoint}")"
    if tcp_reachable_runtime "\${host}" "\${port}"; then
      printf '%s\n' "\${endpoint}"
      return 0
    fi
  done
  if [[ -n "\${first_endpoint}" ]]; then
    printf '%s\n' "\${first_endpoint}"
    return 0
  fi
  return 1
}

update_remote_host_runtime() {
  local endpoint="\$1"
  local host=""
  local port=""
  local deskflow_conf_dir="\${HOME}/.config/Deskflow"
  local deskflow_conf_path="\${deskflow_conf_dir}/Deskflow.conf"
  local tmp_file=""

  read -r host port <<<"\$(split_server_endpoint_runtime "\${endpoint}")"
  [[ -n "\${host}" ]] || return 0

  mkdir -p "\${deskflow_conf_dir}"
  if [[ -f "\${deskflow_conf_path}" ]]; then
    if grep -Eq '^[[:space:]]*remoteHost[[:space:]]*=' "\${deskflow_conf_path}"; then
      tmp_file="\$(mktemp)"
      sed -E "s|^[[:space:]]*remoteHost[[:space:]]*=.*$|remoteHost=\${host}|" "\${deskflow_conf_path}" > "\${tmp_file}"
      mv "\${tmp_file}" "\${deskflow_conf_path}"
    else
      printf '\nremoteHost=%s\n' "\${host}" >> "\${deskflow_conf_path}"
    fi
  else
    printf 'remoteHost=%s\n' "\${host}" > "\${deskflow_conf_path}"
  fi
}

guard_single_client_instance_runtime() {
  local lock_token="${client_runtime_name//[^[:alnum:]_.-]/_}"
  local lock_dir="\${TMPDIR:-/tmp}/deskbridge-deskflow-client-\${lock_token}.lock"
  local lock_pid_file="\${lock_dir}/pid"
  local existing_pid=""

  if mkdir "\${lock_dir}" >/dev/null 2>&1; then
    printf '%s\n' "\$\$" > "\${lock_pid_file}"
    return 0
  fi

  if [[ -f "\${lock_pid_file}" ]]; then
    existing_pid="\$(cat "\${lock_pid_file}" 2>/dev/null || true)"
    if [[ -n "\${existing_pid}" ]] && kill -0 "\${existing_pid}" >/dev/null 2>&1; then
      echo "Deskflow client already running for ${client_runtime_name} (pid=\${existing_pid}); skipping duplicate start."
      exit 0
    fi
  fi

  rm -rf "\${lock_dir}" >/dev/null 2>&1 || true
  if mkdir "\${lock_dir}" >/dev/null 2>&1; then
    printf '%s\n' "\$\$" > "\${lock_pid_file}"
    return 0
  fi

  echo "Could not acquire Deskflow client start lock: \${lock_dir}" >&2
  exit 1
}

guard_single_client_instance_runtime
selected_server="\$(first_reachable_endpoint_runtime "\${server_candidates_csv}" || true)"
if [[ -z "\${selected_server}" ]]; then
  echo "No server address configured (empty endpoint list)" >&2
  exit 1
fi

update_remote_host_runtime "\${selected_server}"
exec "${deskflow_client_bin}" ${deskflow_client_mode} \${new_instance_flag} --no-daemon --name "${client_runtime_name}" "\${selected_server}"
EOF
)"
if [[ -n "${deskflow_client_mode}" ]]; then
  write_start_script "${client_start_script}" "${client_start_body}"
else
  write_start_script "${client_start_script}" "${client_start_body}"
fi
log "client launcher written: ${client_start_script}"
log "start command: ${client_start_script}"

if [[ "${autostart}" == "true" ]]; then
  if [[ "${platform}" == "linux" ]]; then
    install_linux_autostart "client" "${client_start_script}"
  else
    install_macos_autostart "client" "${client_start_script}"
  fi
fi

cat <<EOF

Next steps (client):
1) Start now: ${client_start_script}
2) Verify the server service is running and reachable at ${server_ip}
EOF
