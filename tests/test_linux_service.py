from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


class LinuxServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tempdir = tempfile.TemporaryDirectory()
        config_path = Path(cls._tempdir.name) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "auth_token": "token",
                    "receiver_url": "http://127.0.0.1:8765",
                    "inbox_dir": str(Path(cls._tempdir.name) / "inbox"),
                    "drop_dir": str(Path(cls._tempdir.name) / "drop"),
                    "state_dir": str(Path(cls._tempdir.name) / "state"),
                }
            )
        )
        os.environ["UNIXDROP_CONFIG"] = str(config_path)

        cls.module = importlib.import_module("unixdrop.linux_service")
        cls.module = importlib.reload(cls.module)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tempdir.cleanup()

    def test_conflict_destination_uses_timestamp_suffix(self) -> None:
        inbox = Path(self._tempdir.name) / "conflict-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        original = inbox / "file.txt"
        original.write_text("existing")

        result = self.module.resolve_conflict_destination(
            inbox,
            "file.txt",
            now=datetime(2026, 5, 25, 23, 10, 0),
        )
        self.assertEqual(result.name, "file (conflict 2026-05-25 23-10-00).txt")

    def test_should_open_link_logic(self) -> None:
        self.assertTrue(self.module.should_open_link(True, False))
        self.assertFalse(self.module.should_open_link(False, False))
        self.assertFalse(self.module.should_open_link(True, True))

    def test_conflict_destination_sanitizes_filename(self) -> None:
        inbox = Path(self._tempdir.name) / "sanitize-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        result = self.module.resolve_conflict_destination(inbox, "../danger.txt")
        self.assertEqual(result, inbox / "danger.txt")

    def test_open_link_reports_missing_xdg_open(self) -> None:
        with mock.patch.object(self.module.shutil, "which", return_value=None):
            opened, error = self.module._open_link("https://example.com")
        self.assertFalse(opened)
        self.assertEqual(error, "xdg-open not found")

    def test_open_link_handles_popen_failure(self) -> None:
        with (
            mock.patch.object(self.module.shutil, "which", return_value="/usr/bin/xdg-open"),
            mock.patch.object(self.module.subprocess, "Popen", side_effect=OSError("boom")),
        ):
            opened, error = self.module._open_link("https://example.com")
        self.assertFalse(opened)
        self.assertIn("boom", str(error))


if __name__ == "__main__":
    unittest.main()
