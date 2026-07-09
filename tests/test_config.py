from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from unixdrop.config import deskflow_start_script, load_config, parse_clipboard_mode, parse_deskflow_role


class ConfigTests(unittest.TestCase):
    def _write_temp_config(self, payload: dict) -> Path:
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        try:
            json.dump(payload, handle)
            handle.flush()
            return Path(handle.name)
        finally:
            handle.close()

    def test_clipboard_mode_parser(self) -> None:
        self.assertEqual(parse_clipboard_mode("off"), "off")
        self.assertEqual(parse_clipboard_mode("mac-to-linux"), "mac_to_linux")
        self.assertEqual(parse_clipboard_mode("LINUX_TO_MAC"), "linux_to_mac")
        self.assertEqual(parse_clipboard_mode("two_way"), "two_way")

        with self.assertRaises(ValueError):
            parse_clipboard_mode("invalid")

    def test_deskflow_role_parser(self) -> None:
        self.assertEqual(parse_deskflow_role("off"), "off")
        self.assertEqual(parse_deskflow_role("SERVER"), "server")
        self.assertEqual(parse_deskflow_role("client"), "client")

        with self.assertRaises(ValueError):
            parse_deskflow_role("invalid")

    def test_old_clipboard_keys_migrate_to_mode_and_warn(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "shared_clipboard_enabled": True,
                "clipboard_sync_enabled": False,
            }
        )

        stderr_buffer = io.StringIO()
        with redirect_stderr(stderr_buffer):
            cfg = load_config(config_path)

        self.assertEqual(cfg.clipboard_mode, "two_way")
        self.assertIn("deprecated", stderr_buffer.getvalue().lower())

    def test_old_sync_dir_migrates_to_drop_dir(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "sync_dir": "~/LegacyDrop",
            }
        )
        cfg = load_config(config_path)
        self.assertTrue(str(cfg.drop_dir).endswith("LegacyDrop"))

    def test_missing_folder_keys_use_portable_defaults(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
            }
        )

        cfg = load_config(config_path)

        self.assertTrue(str(cfg.inbox_dir).endswith("UnixDrop/Inbox"))
        self.assertTrue(str(cfg.drop_dir).endswith("UnixDrop/Drop"))
        self.assertTrue(str(cfg.link_log_path).endswith("UnixDrop/Inbox/link-log.jsonl"))

    def test_missing_config_mentions_init_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"

            with self.assertRaises(FileNotFoundError) as ctx:
                load_config(missing)

            self.assertIn("deskbridge init", str(ctx.exception))

    def test_rejects_empty_auth_token(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "",
                "receiver_url": "http://127.0.0.1:8765",
            }
        )

        with self.assertRaises(ValueError) as ctx:
            load_config(config_path)

        self.assertIn("auth_token", str(ctx.exception))

    def test_rejects_invalid_receiver_url(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "not-a-url",
            }
        )

        with self.assertRaises(ValueError) as ctx:
            load_config(config_path)

        self.assertIn("receiver_url", str(ctx.exception))

    def test_rejects_invalid_numeric_limits(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "receiver": {"port": 0},
            }
        )

        with self.assertRaises(ValueError) as ctx:
            load_config(config_path)

        self.assertIn("port", str(ctx.exception))

    def test_warns_for_placeholder_token_on_all_interfaces(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "replace-with-the-same-random-token-on-both-machines",
                "receiver_url": "http://127.0.0.1:8765",
                "receiver": {"listen_host": "0.0.0.0"},
            }
        )

        stderr_buffer = io.StringIO()
        with redirect_stderr(stderr_buffer):
            load_config(config_path)

        self.assertIn("placeholder auth_token", stderr_buffer.getvalue())

    def test_link_log_defaults_to_configured_inbox(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "inbox_dir": "~/UnixDrop/Inbox",
            }
        )

        cfg = load_config(config_path)

        self.assertTrue(str(cfg.link_log_path).endswith("UnixDrop/Inbox/link-log.jsonl"))

    def test_tabs_firefox_debug_url_maps_from_nested_config(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "tabs": {
                    "default_browser": "firefox",
                    "firefox_debug_url": "http://127.0.0.1:9333",
                },
            }
        )

        cfg = load_config(config_path)

        self.assertEqual(cfg.tabs_default_browser, "firefox")
        self.assertEqual(cfg.tabs_firefox_debug_url, "http://127.0.0.1:9333")

    def test_deskflow_nested_keys_map_to_flat_fields(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "deskflow": {
                    "enabled": True,
                    "role": "client",
                    "server_start_script": "~/custom/start-server.sh",
                    "client_start_script": "~/custom/start-client.sh",
                    "mac_start_script": "~/custom/start-mac.sh",
                    "linux_start_script": "~/custom/start-linux.sh",
                },
            }
        )
        cfg = load_config(config_path)
        self.assertTrue(cfg.deskflow_enabled)
        self.assertEqual(cfg.deskflow_role, "client")
        self.assertTrue(str(cfg.deskflow_server_start_script).endswith("custom/start-server.sh"))
        self.assertTrue(str(cfg.deskflow_client_start_script).endswith("custom/start-client.sh"))
        self.assertTrue(str(cfg.deskflow_mac_start_script).endswith("custom/start-mac.sh"))
        self.assertTrue(str(cfg.deskflow_linux_start_script).endswith("custom/start-linux.sh"))

    def test_deskflow_role_selects_start_script(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "deskflow": {
                    "role": "server",
                    "server_start_script": "~/custom/server.sh",
                    "client_start_script": "~/custom/client.sh",
                },
            }
        )
        cfg = load_config(config_path)
        self.assertTrue(str(deskflow_start_script(cfg, "linux")).endswith("custom/server.sh"))


if __name__ == "__main__":
    unittest.main()
