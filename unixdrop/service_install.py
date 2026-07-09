from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path


LINUX_SERVICE_NAME = "unixdrop-receiver.service"
MAC_AGENT_LABEL = "com.unixdrop.agent"


def linux_service_text(python_executable: str | None = None) -> str:
    python = python_executable or sys.executable
    return f"""[Unit]
Description=UnixDrop node service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} -m unixdrop.node
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def mac_agent_payload(python_executable: str | None = None, home_dir: Path | None = None) -> dict:
    python = python_executable or sys.executable
    home = home_dir or Path.home()
    return {
        "Label": MAC_AGENT_LABEL,
        "ProgramArguments": [python, "-m", "unixdrop.node"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(home / "Library" / "Logs" / "unixdrop.log"),
        "StandardErrorPath": str(home / "Library" / "Logs" / "unixdrop.log"),
    }


def write_linux_service(
    *,
    target_dir: Path | None = None,
    python_executable: str | None = None,
) -> Path:
    directory = target_dir or (Path.home() / ".config" / "systemd" / "user")
    target = directory / LINUX_SERVICE_NAME
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(linux_service_text(python_executable), encoding="utf-8")
    return target


def write_mac_agent(
    *,
    target_dir: Path | None = None,
    python_executable: str | None = None,
    home_dir: Path | None = None,
) -> Path:
    directory = target_dir or (Path.home() / "Library" / "LaunchAgents")
    target = directory / f"{MAC_AGENT_LABEL}.plist"
    directory.mkdir(parents=True, exist_ok=True)
    payload = mac_agent_payload(python_executable, home_dir)
    with target.open("wb") as handle:
        plistlib.dump(payload, handle)
    return target


def install_linux_service(python_executable: str | None = None) -> Path:
    target = write_linux_service(python_executable=python_executable)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return target


def install_mac_agent(python_executable: str | None = None) -> Path:
    return write_mac_agent(python_executable=python_executable)
