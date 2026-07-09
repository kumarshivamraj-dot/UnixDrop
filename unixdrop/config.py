from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_CONFIG_PATH = Path("~/.config/unixdrop/config.json").expanduser()
ENV_CONFIG_PATH = "UNIXDROP_CONFIG"


CLIPBOARD_MODES = {
    "off",
    "mac_to_linux",
    "linux_to_mac",
    "two_way",
}

DESKFLOW_ROLES = {
    "off",
    "server",
    "client",
}


@dataclass
class AppConfig:
    auth_token: str
    listen_host: str = "0.0.0.0"
    port: int = 8765
    receiver_url: str = "http://127.0.0.1:8765"
    inbox_dir: Path = Path("~/UnixDrop/Inbox").expanduser()
    drop_dir: Path = Path("~/UnixDrop/Drop").expanduser()
    link_log_path: Path = Path("~/UnixDrop/Inbox/link-log.jsonl").expanduser()
    state_dir: Path = Path("~/.local/state/unixdrop").expanduser()
    auto_open_links: bool = True
    clipboard_mode: str = "off"
    max_clipboard_chars: int = 20000
    clipboard_poll_seconds: int = 2
    file_poll_seconds: int = 5
    request_timeout_seconds: int = 15
    delete_after_send: bool = False
    max_file_mb: int = 500
    tabs_default_browser: str = "auto"
    tabs_firefox_debug_url: str = "http://127.0.0.1:9222"
    obsidian_enabled: bool = False
    obsidian_vault_dir: Path = Path("~/Obsidian/MainVault").expanduser()
    obsidian_remote_vault: str = ""
    obsidian_conflict_strategy: str = "copy"
    obsidian_poll_seconds: int = 10
    obsidian_excludes: list[str] | None = None
    deskflow_enabled: bool = False
    deskflow_role: str = "off"
    deskflow_server_start_script: Path = Path("~/.config/deskflow/start-deskflow-server.sh").expanduser()
    deskflow_client_start_script: Path = Path("~/.config/deskflow/start-deskflow-client.sh").expanduser()
    deskflow_mac_start_script: Path = Path("~/.config/deskflow/start-deskflow-server.sh").expanduser()
    deskflow_linux_start_script: Path = Path("~/.config/deskflow/start-deskflow-client.sh").expanduser()


def parse_clipboard_mode(mode: str) -> str:
    normalized = str(mode).strip().lower().replace("-", "_")
    if normalized not in CLIPBOARD_MODES:
        raise ValueError(f"invalid clipboard mode: {mode}")
    return normalized


def parse_deskflow_role(role: str) -> str:
    normalized = str(role).strip().lower().replace("-", "_")
    if normalized not in DESKFLOW_ROLES:
        raise ValueError(f"invalid deskflow role: {role}")
    return normalized


def _warn(message: str) -> None:
    print(f"[unixdrop config] {message}", file=sys.stderr)


def _require_positive_int(raw: object, name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _require_nonempty_string(raw: object, name: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL bytes")
    return value


def _validate_receiver_url(raw: object) -> str:
    value = _require_nonempty_string(raw, "receiver_url")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("receiver_url must be an http(s) URL with a host")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"receiver_url has invalid port: {exc}") from None
    return value


def _validate_path(value: Path, name: str) -> Path:
    if "\x00" in str(value):
        raise ValueError(f"{name} must not contain NUL bytes")
    return value


def _placeholder_token(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {
        "changeme",
        "change-me",
        "change_me",
        "replace-me",
        "default-token",
        "replace-with-the-same-random-token-on-both-machines",
    }


def _flatten_config(raw: dict) -> tuple[dict, list[str]]:
    flat = dict(raw)
    warnings: list[str] = []

    receiver = raw.get("receiver") if isinstance(raw.get("receiver"), dict) else {}
    clipboard = raw.get("clipboard") if isinstance(raw.get("clipboard"), dict) else {}
    drop = raw.get("drop") if isinstance(raw.get("drop"), dict) else {}
    tabs = raw.get("tabs") if isinstance(raw.get("tabs"), dict) else {}
    obsidian = raw.get("obsidian") if isinstance(raw.get("obsidian"), dict) else {}
    deskflow = raw.get("deskflow") if isinstance(raw.get("deskflow"), dict) else {}

    if "host" in receiver and "receiver_url" not in flat:
        host = str(receiver.get("host", "127.0.0.1")).strip()
        port = int(receiver.get("port", flat.get("port", 8765)))
        flat["receiver_url"] = f"http://{host}:{port}"
    if "listen_host" in receiver and "listen_host" not in flat:
        flat["listen_host"] = receiver["listen_host"]
    if "port" in receiver and "port" not in flat:
        flat["port"] = receiver["port"]
    if "auto_open_links" in receiver and "auto_open_links" not in flat:
        flat["auto_open_links"] = receiver["auto_open_links"]
    if "inbox_dir" in receiver and "inbox_dir" not in flat:
        flat["inbox_dir"] = receiver["inbox_dir"]
    if "linux_inbox" in receiver and "inbox_dir" not in flat:
        flat["inbox_dir"] = receiver["linux_inbox"]

    if "mode" in clipboard and "clipboard_mode" not in flat:
        flat["clipboard_mode"] = clipboard["mode"]
    if "max_chars" in clipboard and "max_clipboard_chars" not in flat:
        flat["max_clipboard_chars"] = clipboard["max_chars"]

    if "folder" in drop and "drop_dir" not in flat:
        flat["drop_dir"] = drop["folder"]
    if "delete_after_send" in drop and "delete_after_send" not in flat:
        flat["delete_after_send"] = drop["delete_after_send"]
    if "max_file_mb" in drop and "max_file_mb" not in flat:
        flat["max_file_mb"] = drop["max_file_mb"]

    if "default_browser" in tabs and "tabs_default_browser" not in flat:
        flat["tabs_default_browser"] = tabs["default_browser"]
    if "firefox_debug_url" in tabs and "tabs_firefox_debug_url" not in flat:
        flat["tabs_firefox_debug_url"] = tabs["firefox_debug_url"]

    if "enabled" in obsidian and "obsidian_enabled" not in flat:
        flat["obsidian_enabled"] = obsidian["enabled"]
    if "local_vault" in obsidian and "obsidian_vault_dir" not in flat:
        flat["obsidian_vault_dir"] = obsidian["local_vault"]
    if "remote_vault" in obsidian and "obsidian_remote_vault" not in flat:
        flat["obsidian_remote_vault"] = obsidian["remote_vault"]
    if "conflict_strategy" in obsidian and "obsidian_conflict_strategy" not in flat:
        flat["obsidian_conflict_strategy"] = obsidian["conflict_strategy"]

    if "enabled" in deskflow and "deskflow_enabled" not in flat:
        flat["deskflow_enabled"] = deskflow["enabled"]
    if "role" in deskflow and "deskflow_role" not in flat:
        flat["deskflow_role"] = deskflow["role"]
    if "server_start_script" in deskflow and "deskflow_server_start_script" not in flat:
        flat["deskflow_server_start_script"] = deskflow["server_start_script"]
    if "client_start_script" in deskflow and "deskflow_client_start_script" not in flat:
        flat["deskflow_client_start_script"] = deskflow["client_start_script"]
    if "mac_start_script" in deskflow and "deskflow_mac_start_script" not in flat:
        flat["deskflow_mac_start_script"] = deskflow["mac_start_script"]
    if "mac_start_script" in deskflow and "deskflow_server_start_script" not in flat:
        flat["deskflow_server_start_script"] = deskflow["mac_start_script"]
    if "linux_start_script" in deskflow and "deskflow_linux_start_script" not in flat:
        flat["deskflow_linux_start_script"] = deskflow["linux_start_script"]
    if "linux_start_script" in deskflow and "deskflow_client_start_script" not in flat:
        flat["deskflow_client_start_script"] = deskflow["linux_start_script"]

    if "shared_clipboard_enabled" in raw or "clipboard_sync_enabled" in raw:
        warnings.append(
            "`shared_clipboard_enabled` and `clipboard_sync_enabled` are deprecated; use `clipboard_mode`."
        )
        if "clipboard_mode" not in flat:
            shared = bool(raw.get("shared_clipboard_enabled", False))
            old_clip = bool(raw.get("clipboard_sync_enabled", False))
            if shared:
                flat["clipboard_mode"] = "two_way"
            elif old_clip:
                flat["clipboard_mode"] = "mac_to_linux"
            else:
                flat["clipboard_mode"] = "off"

    if "sync_dir" in raw and "drop_dir" not in flat:
        warnings.append("`sync_dir` is deprecated; use `drop_dir`.")
        flat["drop_dir"] = raw["sync_dir"]

    return flat, warnings


def _apply_paths(raw: dict) -> dict:
    converted = dict(raw)
    converted["inbox_dir"] = Path(raw.get("inbox_dir", "~/UnixDrop/Inbox")).expanduser()
    converted["drop_dir"] = Path(raw.get("drop_dir", "~/UnixDrop/Drop")).expanduser()
    default_link_log = converted["inbox_dir"] / "link-log.jsonl"
    converted["link_log_path"] = Path(raw.get("link_log_path", default_link_log)).expanduser()
    converted["state_dir"] = Path(raw.get("state_dir", "~/.local/state/unixdrop")).expanduser()
    converted["obsidian_vault_dir"] = Path(raw.get("obsidian_vault_dir", "~/Obsidian/MainVault")).expanduser()
    converted["deskflow_server_start_script"] = Path(
        raw.get("deskflow_server_start_script", "~/.config/deskflow/start-deskflow-server.sh")
    ).expanduser()
    converted["deskflow_client_start_script"] = Path(
        raw.get("deskflow_client_start_script", "~/.config/deskflow/start-deskflow-client.sh")
    ).expanduser()
    converted["deskflow_mac_start_script"] = Path(
        raw.get("deskflow_mac_start_script", "~/.config/deskflow/start-deskflow-server.sh")
    ).expanduser()
    converted["deskflow_linux_start_script"] = Path(
        raw.get("deskflow_linux_start_script", "~/.config/deskflow/start-deskflow-client.sh")
    ).expanduser()
    return converted


def load_config(config_path: Path | None = None) -> AppConfig:
    env_path = os.environ.get(ENV_CONFIG_PATH)
    path = (config_path or (Path(env_path) if env_path else DEFAULT_CONFIG_PATH)).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Run `deskbridge init` to create one, "
            "or copy config.example.json to this path first."
        )

    loaded = json.loads(path.read_text(encoding="utf-8"))
    flattened, warnings = _flatten_config(loaded)
    prepared = _apply_paths(flattened)

    prepared["auth_token"] = _require_nonempty_string(prepared.get("auth_token", ""), "auth_token")
    prepared["receiver_url"] = _validate_receiver_url(prepared.get("receiver_url", ""))
    prepared["listen_host"] = _require_nonempty_string(prepared.get("listen_host", "0.0.0.0"), "listen_host")
    prepared["port"] = _require_positive_int(prepared.get("port", 8765), "port", maximum=65535)
    prepared["clipboard_poll_seconds"] = _require_positive_int(
        prepared.get("clipboard_poll_seconds", 2),
        "clipboard_poll_seconds",
    )
    prepared["file_poll_seconds"] = _require_positive_int(
        prepared.get("file_poll_seconds", 5),
        "file_poll_seconds",
    )
    prepared["request_timeout_seconds"] = _require_positive_int(
        prepared.get("request_timeout_seconds", 15),
        "request_timeout_seconds",
    )
    prepared["obsidian_poll_seconds"] = _require_positive_int(
        prepared.get("obsidian_poll_seconds", 10),
        "obsidian_poll_seconds",
    )
    prepared["clipboard_mode"] = parse_clipboard_mode(prepared.get("clipboard_mode", "off"))
    prepared["deskflow_role"] = parse_deskflow_role(prepared.get("deskflow_role", "off"))
    prepared["max_clipboard_chars"] = _require_positive_int(
        prepared.get("max_clipboard_chars", 20000),
        "max_clipboard_chars",
    )
    prepared["max_file_mb"] = _require_positive_int(prepared.get("max_file_mb", 500), "max_file_mb")
    prepared["obsidian_conflict_strategy"] = str(prepared.get("obsidian_conflict_strategy", "copy"))
    prepared["inbox_dir"] = _validate_path(prepared["inbox_dir"], "inbox_dir")
    prepared["drop_dir"] = _validate_path(prepared["drop_dir"], "drop_dir")
    prepared["link_log_path"] = _validate_path(prepared["link_log_path"], "link_log_path")
    prepared["state_dir"] = _validate_path(prepared["state_dir"], "state_dir")
    prepared["obsidian_vault_dir"] = _validate_path(prepared["obsidian_vault_dir"], "obsidian_vault_dir")

    allowed = set(AppConfig.__dataclass_fields__.keys())
    filtered = {key: value for key, value in prepared.items() if key in allowed}

    for warning in warnings:
        _warn(warning)
    if str(filtered.get("listen_host")) in {"0.0.0.0", "::"} and _placeholder_token(str(filtered["auth_token"])):
        _warn("receiver listens on all interfaces with a placeholder auth_token; generate a unique token.")

    return AppConfig(**filtered)


def clipboard_send_enabled(mode: str) -> bool:
    parsed = parse_clipboard_mode(mode)
    return parsed in {"mac_to_linux", "two_way"}


def clipboard_pull_enabled(mode: str) -> bool:
    parsed = parse_clipboard_mode(mode)
    return parsed in {"linux_to_mac", "two_way"}


def deskflow_start_script(config: AppConfig, platform: str | None = None) -> Path | None:
    role = parse_deskflow_role(config.deskflow_role)
    if role == "server":
        return config.deskflow_server_start_script
    if role == "client":
        return config.deskflow_client_start_script
    if not config.deskflow_enabled:
        return None
    if platform == "darwin":
        return config.deskflow_mac_start_script
    if platform and platform.startswith("linux"):
        return config.deskflow_linux_start_script
    return None
