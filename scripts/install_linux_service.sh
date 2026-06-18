#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="${HOME}/.config/systemd/user"
TARGET_FILE="${TARGET_DIR}/unixdrop-receiver.service"

mkdir -p "${TARGET_DIR}"
sed "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
  "${PROJECT_DIR}/systemd/unixdrop-receiver.service" > "${TARGET_FILE}"

systemctl --user daemon-reload
echo "Installed UnixDrop node service: ${TARGET_FILE}"
echo "Next: systemctl --user enable --now unixdrop-receiver.service"
