from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG_PATH = Path("~/.config/unixdrop/config.json").expanduser()


@dataclass
class AppConfig:
    auth_token: str
    listen_host: str = "0.0.0.0"
    port: int = 8765
    receiver_url: str = "http://127.0.0.1:8765"
    inbox_dir: Path = Path("~/UnixDrop/inbox").expanduser()
    sync_dir: Path = Path("~/UnixDrop/outbox").expanduser()
    link_log_path: Path = Path("~/UnixDrop/link-log.jsonl").expanduser()
    state_dir: Path = Path("~/.local/state/unixdrop").expanduser()
    auto_open_links: bool = True
    clipboard_poll_seconds: int = 2
    file_poll_seconds: int = 5
    request_timeout_seconds: int = 15
    obsidian_enabled: bool = False
    obsidian_vault_dir: Path = Path("~/Obsidian/MainVault").expanduser()
    obsidian_poll_seconds: int = 10
    obsidian_excludes: list[str] | None = None


def load_config(config_path: Path | None = None) -> AppConfig:
    path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.json to this path first."
        )

    raw = json.loads(path.read_text())
    raw["inbox_dir"] = Path(raw.get("inbox_dir", "~/UnixDrop/inbox")).expanduser()
    raw["sync_dir"] = Path(raw.get("sync_dir", "~/UnixDrop/outbox")).expanduser()
    raw["link_log_path"] = Path(raw.get("link_log_path", "~/UnixDrop/link-log.jsonl")).expanduser()
    raw["state_dir"] = Path(raw.get("state_dir", "~/.local/state/unixdrop")).expanduser()
    raw["obsidian_vault_dir"] = Path(raw.get("obsidian_vault_dir", "~/Obsidian/MainVault")).expanduser()
    return AppConfig(**raw)
