#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


CLIPBOARD_MODES = {"off", "mac_to_linux", "linux_to_mac", "two_way"}


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _parse_receiver(raw: dict) -> tuple[str, int]:
    receiver_url = str(raw.get("receiver_url", "http://127.0.0.1:8765")).strip()
    parsed = urlparse(receiver_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or int(raw.get("port", 8765))

    receiver = raw.get("receiver") if isinstance(raw.get("receiver"), dict) else {}
    host = str(receiver.get("host", host))
    port = int(receiver.get("port", port))
    return host, port


def _parse_clipboard_mode(raw: dict) -> str:
    clipboard = raw.get("clipboard") if isinstance(raw.get("clipboard"), dict) else {}
    mode = str(raw.get("clipboard_mode", clipboard.get("mode", ""))).strip().lower().replace("-", "_")
    if mode in CLIPBOARD_MODES:
        return mode

    shared = _as_bool(raw.get("shared_clipboard_enabled"), default=False)
    old_sync = _as_bool(raw.get("clipboard_sync_enabled"), default=False)
    if shared:
        return "two_way"
    if old_sync:
        return "mac_to_linux"
    return "off"


def migrate(raw: dict) -> dict:
    receiver = raw.get("receiver") if isinstance(raw.get("receiver"), dict) else {}
    clipboard = raw.get("clipboard") if isinstance(raw.get("clipboard"), dict) else {}
    drop = raw.get("drop") if isinstance(raw.get("drop"), dict) else {}
    tabs = raw.get("tabs") if isinstance(raw.get("tabs"), dict) else {}
    obsidian = raw.get("obsidian") if isinstance(raw.get("obsidian"), dict) else {}

    host, port = _parse_receiver(raw)
    inbox_dir = str(raw.get("inbox_dir", receiver.get("linux_inbox", "~/Inbox/MacDrop")))

    migrated = {
        "auth_token": raw["auth_token"],
        "receiver": {
            "host": host,
            "port": port,
            "listen_host": str(raw.get("listen_host", receiver.get("listen_host", "0.0.0.0"))),
            "auto_open_links": _as_bool(raw.get("auto_open_links", receiver.get("auto_open_links", True)), True),
            "linux_inbox": inbox_dir,
        },
        "receiver_url": str(raw.get("receiver_url", f"http://{host}:{port}")),
        "clipboard": {
            "mode": _parse_clipboard_mode(raw),
            "max_chars": int(raw.get("max_clipboard_chars", clipboard.get("max_chars", 20000))),
        },
        "drop": {
            "folder": str(raw.get("drop_dir", drop.get("folder", raw.get("sync_dir", "~/Drop to ThinkPad")))),
            "delete_after_send": _as_bool(raw.get("delete_after_send", drop.get("delete_after_send", False))),
            "max_file_mb": int(raw.get("max_file_mb", drop.get("max_file_mb", 500))),
        },
        "tabs": {
            "default_browser": str(raw.get("tabs_default_browser", tabs.get("default_browser", "auto"))),
        },
        "link_log_path": str(raw.get("link_log_path", "~/Inbox/MacDrop/link-log.jsonl")),
        "state_dir": str(raw.get("state_dir", "~/.local/state/unixdrop")),
        "clipboard_poll_seconds": int(raw.get("clipboard_poll_seconds", 2)),
        "file_poll_seconds": int(raw.get("file_poll_seconds", 5)),
        "request_timeout_seconds": int(raw.get("request_timeout_seconds", 15)),
        "obsidian": {
            "enabled": _as_bool(raw.get("obsidian_enabled", obsidian.get("enabled", False))),
            "local_vault": str(raw.get("obsidian_vault_dir", obsidian.get("local_vault", "~/Obsidian/MainVault"))),
            "remote_vault": str(raw.get("obsidian_remote_vault", obsidian.get("remote_vault", ""))),
            "conflict_strategy": str(raw.get("obsidian_conflict_strategy", obsidian.get("conflict_strategy", "copy"))),
        },
        "obsidian_poll_seconds": int(raw.get("obsidian_poll_seconds", 10)),
        "obsidian_excludes": raw.get(
            "obsidian_excludes",
            [
                ".obsidian/workspace.json",
                ".obsidian/workspaces.json",
                ".obsidian/cache",
                ".trash",
            ],
        ),
    }
    return migrated


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate UnixDrop config to deskbridge-oriented schema")
    parser.add_argument("--path", default="~/.config/unixdrop/config.json", help="Path to config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Print migrated JSON without writing")
    args = parser.parse_args()

    path = Path(args.path).expanduser()
    if not path.exists():
        raise SystemExit(f"config not found: {path}")

    raw = json.loads(path.read_text())
    if "auth_token" not in raw:
        raise SystemExit("config must include auth_token")

    migrated = migrate(raw)

    if args.dry_run:
        print(json.dumps(migrated, indent=2))
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    backup = path.with_name(path.name + f".backup-{stamp}")
    backup.write_text(path.read_text())
    path.write_text(json.dumps(migrated, indent=2) + "\n")

    print(f"Backup: {backup}")
    print(f"Updated: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
