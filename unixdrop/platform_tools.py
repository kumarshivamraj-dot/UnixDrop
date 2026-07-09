from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def platform_family(platform: str | None = None) -> str | None:
    value = platform or sys.platform
    if value in {"darwin", "macos"}:
        return "macos"
    if value.startswith("linux"):
        return "linux"
    return None


def service_manager_command(platform: str | None = None) -> list[str] | None:
    family = platform_family(platform)
    if family == "macos":
        return ["launchctl"]
    if family == "linux":
        return ["systemctl", "--user"]
    return None


def service_manager_status(platform: str | None = None) -> tuple[bool, str]:
    command = service_manager_command(platform)
    if command is None:
        return False, f"unsupported platform: {platform or sys.platform}"
    found = shutil.which(command[0])
    if found:
        return True, f"{' '.join(command)} available at {found}"
    return False, f"{command[0]} not found"


def link_opener_command(platform: str | None = None) -> list[str] | None:
    family = platform_family(platform)
    if family == "macos":
        return [shutil.which("open") or "open"]
    if family == "linux":
        opener = shutil.which("xdg-open")
        if opener:
            return [opener]
    return None


def link_opener_status(platform: str | None = None) -> tuple[bool, str]:
    command = link_opener_command(platform)
    if command is None:
        return False, f"link opener not found for {platform or sys.platform}"
    executable = command[0]
    found = shutil.which(executable) or executable
    return True, f"{Path(found).name} available"


def clipboard_get_command(platform: str | None = None) -> list[str] | None:
    family = platform_family(platform)
    if family == "macos" and shutil.which("pbpaste"):
        return ["pbpaste"]
    if shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"]
    return None


def clipboard_set_command(platform: str | None = None) -> list[str] | None:
    family = platform_family(platform)
    if family == "macos" and shutil.which("pbcopy"):
        return ["pbcopy"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def clipboard_tools_status(platform: str | None = None) -> tuple[bool, str]:
    getter = clipboard_get_command(platform)
    setter = clipboard_set_command(platform)
    if getter and setter:
        return True, f"read: {getter[0]}, write: {setter[0]}"
    if getter:
        return False, f"read only: {getter[0]}"
    if setter:
        return False, f"write only: {setter[0]}"
    return False, "no clipboard read/write tool found"


def find_deskflow_binary(platform: str | None, binary_name: str) -> str | None:
    found = shutil.which(binary_name)
    if found:
        return found
    if platform_family(platform) == "macos":
        mac_path = Path("/Applications/Deskflow.app/Contents/MacOS") / binary_name
        if mac_path.exists() and os.access(mac_path, os.X_OK):
            return str(mac_path)
    return None


def resolve_deskflow_command(role: str, platform: str | None = None) -> tuple[str, str] | None:
    if role == "server":
        dedicated = find_deskflow_binary(platform, "deskflow-server")
        if dedicated:
            return dedicated, ""
        core = find_deskflow_binary(platform, "deskflow-core")
        if core:
            return core, "server"
        return None

    if role == "client":
        dedicated = find_deskflow_binary(platform, "deskflow-client")
        if dedicated:
            return dedicated, ""
        core = find_deskflow_binary(platform, "deskflow-core")
        if core:
            return core, "client"
    return None


def deskflow_binary_status(platform: str | None = None) -> tuple[bool, str]:
    for role in ("server", "client"):
        command = resolve_deskflow_command(role, platform)
        if command is not None:
            binary, mode = command
            suffix = f" ({mode} mode)" if mode else ""
            return True, f"{Path(binary).name}{suffix} available at {binary}"
    return False, "deskflow-server, deskflow-client, or deskflow-core not found"
