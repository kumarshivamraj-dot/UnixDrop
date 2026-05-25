from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
