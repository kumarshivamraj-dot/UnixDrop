from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from unixdrop.config import load_config, parse_clipboard_mode


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

    def test_deskflow_nested_keys_map_to_flat_fields(self) -> None:
        config_path = self._write_temp_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "deskflow": {
                    "enabled": True,
                    "mac_start_script": "~/custom/start-mac.sh",
                    "linux_start_script": "~/custom/start-linux.sh",
                },
            }
        )
        cfg = load_config(config_path)
        self.assertTrue(cfg.deskflow_enabled)
        self.assertTrue(str(cfg.deskflow_mac_start_script).endswith("custom/start-mac.sh"))
        self.assertTrue(str(cfg.deskflow_linux_start_script).endswith("custom/start-linux.sh"))


if __name__ == "__main__":
    unittest.main()
