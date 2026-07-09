#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${script_dir}/.." && pwd)"
export PYTHONPATH="${project_dir}${PYTHONPATH:+:${PYTHONPATH}}"

exec "${PYTHON:-python3}" -m unixdrop.deskflow_setup "$@"
