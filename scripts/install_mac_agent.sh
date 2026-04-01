#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="${HOME}/Library/LaunchAgents"
TARGET_FILE="${TARGET_DIR}/com.unixdrop.agent.plist"

mkdir -p "${TARGET_DIR}"
sed \
  -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
  -e "s#__HOME_DIR__#${HOME}#g" \
  "${PROJECT_DIR}/launchd/com.unixdrop.agent.plist" > "${TARGET_FILE}"

launchctl unload "${TARGET_FILE}" >/dev/null 2>&1 || true

echo "Installed ${TARGET_FILE}"
echo "Next: launchctl load ${TARGET_FILE}"
