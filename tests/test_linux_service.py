from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from hashlib import sha256
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

    def test_publish_temp_file_uses_conflict_name_without_overwrite(self) -> None:
        inbox = Path(self._tempdir.name) / "atomic-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        existing = inbox / "report.txt"
        existing.write_text("existing", encoding="utf-8")
        temp_path = inbox / ".upload.tmp"
        temp_path.write_text("incoming", encoding="utf-8")

        with mock.patch.object(self.module, "datetime") as datetime_mock:
            datetime_mock.now.return_value = datetime(2026, 5, 25, 23, 10, 0)
            published = self.module._publish_temp_file_unique(temp_path, inbox, "report.txt")

        self.assertEqual(existing.read_text(encoding="utf-8"), "existing")
        self.assertEqual(published.name, "report (conflict 2026-05-25 23-10-00).txt")
        self.assertEqual(published.read_text(encoding="utf-8"), "incoming")

    def test_open_link_reports_missing_xdg_open(self) -> None:
        with mock.patch.object(self.module.shutil, "which", return_value=None):
            opened, error = self.module._open_link("https://example.com")
        self.assertFalse(opened)
        self.assertEqual(error, "link opener not found")

    def test_open_link_handles_popen_failure(self) -> None:
        with (
            mock.patch.object(self.module.shutil, "which", return_value="/usr/bin/xdg-open"),
            mock.patch.object(self.module.subprocess, "Popen", side_effect=OSError("boom")),
        ):
            opened, error = self.module._open_link("https://example.com")
        self.assertFalse(opened)
        self.assertIn("boom", str(error))

    def test_health_check_clipboard_payload_is_not_stored(self) -> None:
        with (
            mock.patch.object(self.module, "_update_clipboard_state") as update_mock,
            mock.patch.object(self.module, "_write_linux_clipboard") as write_mock,
        ):
            result = self.module._store_clipboard_payload("deskbridge-health-probe", "health-check")

        self.assertEqual(result["hash"], sha256(b"deskbridge-health-probe").hexdigest())
        self.assertFalse(result["stored"])
        update_mock.assert_not_called()
        write_mock.assert_not_called()

    def test_stale_health_probe_text_is_not_stored_as_user_clipboard(self) -> None:
        with (
            mock.patch.object(self.module, "_update_clipboard_state") as update_mock,
            mock.patch.object(self.module, "_write_linux_clipboard") as write_mock,
        ):
            result = self.module._store_clipboard_payload("deskbridge-health-stale", "local")

        self.assertFalse(result["stored"])
        update_mock.assert_not_called()
        write_mock.assert_not_called()

    def test_normal_clipboard_payload_is_stored(self) -> None:
        with (
            mock.patch.object(self.module, "_update_clipboard_state") as update_mock,
            mock.patch.object(self.module, "_write_linux_clipboard") as write_mock,
        ):
            result = self.module._store_clipboard_payload("copied just now", "local")

        self.assertTrue(result["stored"])
        update_mock.assert_called_once_with("copied just now", "local")
        write_mock.assert_called_once_with("copied just now")

    def test_deskflow_supervisor_stops_after_clean_launcher_exit(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 0
        self.module.DESKFLOW_PROCESS = process
        self.module.DESKFLOW_RETRY_AFTER = 0.0
        self.module.DESKFLOW_SUPERVISION_DISABLED = False

        try:
            with (
                mock.patch.object(self.module, "deskflow_start_script", return_value=Path("/tmp/start-deskflow-client.sh")),
                mock.patch.object(self.module, "_start_deskflow_process") as start_mock,
                mock.patch.object(self.module, "_log"),
            ):
                self.module._ensure_deskflow_running()
                self.module._ensure_deskflow_running()

            self.assertIsNone(self.module.DESKFLOW_PROCESS)
            self.assertTrue(self.module.DESKFLOW_SUPERVISION_DISABLED)
            start_mock.assert_not_called()
        finally:
            self.module.DESKFLOW_PROCESS = None
            self.module.DESKFLOW_RETRY_AFTER = 0.0
            self.module.DESKFLOW_SUPERVISION_DISABLED = False

    def test_vault_atomic_write_replaces_existing_file(self) -> None:
        vault = Path(self._tempdir.name) / "atomic-vault"
        destination = vault / "nested" / "note.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("old", encoding="utf-8")

        self.module.write_bytes_atomic(destination, b"incoming", 123.0)

        self.assertEqual(destination.read_bytes(), b"incoming")
        self.assertAlmostEqual(destination.stat().st_mtime, 123.0, delta=1.0)
        self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])

    def test_vault_path_rejects_sibling_prefix_escape(self) -> None:
        root = Path(self._tempdir.name)
        vault = root / "vault"
        vault.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(ValueError):
            self.module.vault_path(vault, "../vault2/note.md")

        self.assertEqual(
            self.module.vault_path(vault, "nested/note.md"),
            (vault / "nested" / "note.md").resolve(strict=False),
        )


if __name__ == "__main__":
    unittest.main()
