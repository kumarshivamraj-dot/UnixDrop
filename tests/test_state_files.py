from __future__ import annotations

import importlib
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class StateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "auth_token": "token",
                    "receiver_url": "http://127.0.0.1:8765",
                    "drop_dir": str(self.root / "drop"),
                    "inbox_dir": str(self.root / "inbox"),
                    "state_dir": str(self.root / "state"),
                }
            ),
            encoding="utf-8",
        )
        os.environ["UNIXDROP_CONFIG"] = str(config_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_status_ignores_corrupt_state_file(self) -> None:
        status = importlib.import_module("unixdrop.status")
        status = importlib.reload(status)
        state_file = self.root / "state" / "mac_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('{"last_upload_result": "unterminated', encoding="utf-8")

        self.assertEqual(status._read_state(state_file), {})

    def test_mac_agent_recovers_from_corrupt_state_and_saves_valid_json(self) -> None:
        mac_agent = importlib.import_module("unixdrop.mac_agent")
        mac_agent = importlib.reload(mac_agent)
        mac_agent.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        mac_agent.STATE_FILE.write_text('{"last_upload_result": "unterminated', encoding="utf-8")

        state = mac_agent._load_state()
        self.assertEqual(state["last_upload_result"], "none")

        state["last_upload_result"] = "ok"
        mac_agent._save_state(state)
        self.assertEqual(json.loads(mac_agent.STATE_FILE.read_text(encoding="utf-8"))["last_upload_result"], "ok")

    def test_mac_agent_stops_retrying_after_clean_deskflow_launcher_exit(self) -> None:
        mac_agent = importlib.import_module("unixdrop.mac_agent")
        mac_agent = importlib.reload(mac_agent)
        process = mock.Mock()
        process.poll.return_value = 0
        mac_agent._DESKFLOW_RETRY_AFTER = 0.0
        mac_agent._DESKFLOW_SUPERVISION_DISABLED = False

        with (
            mock.patch.object(mac_agent, "deskflow_start_script", return_value=Path("/tmp/start-deskflow-client.sh")),
            mock.patch.object(mac_agent, "_start_deskflow_process") as start_mock,
            mock.patch("builtins.print"),
        ):
            self.assertIsNone(mac_agent._ensure_deskflow_running(process))
            self.assertIsNone(mac_agent._ensure_deskflow_running(None))

        self.assertTrue(mac_agent._DESKFLOW_SUPERVISION_DISABLED)
        start_mock.assert_not_called()

    def test_mac_agent_does_not_push_health_probe_clipboard_text(self) -> None:
        mac_agent = importlib.import_module("unixdrop.mac_agent")
        mac_agent = importlib.reload(mac_agent)
        mac_agent.CONFIG.clipboard_mode = "two_way"
        state = {}

        with (
            mock.patch.object(mac_agent, "_clipboard_text", return_value="deskbridge-health-stale"),
            mock.patch.object(mac_agent, "_post_json") as post_mock,
        ):
            mac_agent._sync_clipboard_push(state)

        self.assertEqual(state["last_local_clipboard_hash"], hashlib.sha256(b"deskbridge-health-stale").hexdigest())
        post_mock.assert_not_called()

    def test_mac_agent_does_not_pull_health_probe_clipboard_text(self) -> None:
        mac_agent = importlib.import_module("unixdrop.mac_agent")
        mac_agent = importlib.reload(mac_agent)
        mac_agent.CONFIG.clipboard_mode = "two_way"
        text = "deskbridge-health-stale"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        state = {}

        with (
            mock.patch.object(mac_agent, "_fetch_json", return_value={"text": text, "hash": digest}),
            mock.patch.object(mac_agent, "_set_clipboard_text") as set_mock,
        ):
            mac_agent._pull_remote_clipboard(state)

        self.assertEqual(state["last_remote_clipboard_hash"], digest)
        set_mock.assert_not_called()

    def test_mac_agent_peer_failure_sets_retry_backoff(self) -> None:
        mac_agent = importlib.import_module("unixdrop.mac_agent")
        mac_agent = importlib.reload(mac_agent)
        mac_agent._PEER_RETRY_AFTER = 0.0
        mac_agent._PEER_LAST_ERROR = ""

        with mock.patch("builtins.print") as print_mock:
            mac_agent._record_peer_request_failure(ConnectionRefusedError("connection refused"))

        self.assertFalse(mac_agent._peer_request_allowed())
        print_mock.assert_called_once()

        mac_agent._record_peer_request_success()
        self.assertTrue(mac_agent._peer_request_allowed())

    def test_mac_agent_reloads_config_when_file_changes(self) -> None:
        mac_agent = importlib.import_module("unixdrop.mac_agent")
        mac_agent = importlib.reload(mac_agent)
        config_path = Path(os.environ["UNIXDROP_CONFIG"])
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["receiver_url"] = "http://192.0.2.55:8765"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        mac_agent.CONFIG_MTIME_NS = 0
        mac_agent._PEER_RETRY_AFTER = 999999999.0

        self.assertTrue(mac_agent._reload_config_if_changed())

        self.assertEqual(mac_agent.CONFIG.receiver_url, "http://192.0.2.55:8765")
        self.assertTrue(mac_agent._peer_request_allowed())

    def test_mac_agent_conflict_copy_uses_atomic_vault_write(self) -> None:
        mac_agent = importlib.import_module("unixdrop.mac_agent")
        mac_agent = importlib.reload(mac_agent)
        note_path = self.root / "vault" / "note.md"

        with mock.patch.object(mac_agent, "write_bytes_atomic") as write_mock:
            mac_agent._write_conflict_copy(note_path, b"incoming", "abcdef0123456789")

        write_mock.assert_called_once_with(
            self.root / "vault" / "note.linux-conflict-abcdef01.md",
            b"incoming",
        )

    def test_mac_agent_skips_unsafe_remote_vault_manifest_path(self) -> None:
        mac_agent = importlib.import_module("unixdrop.mac_agent")
        mac_agent = importlib.reload(mac_agent)
        mac_agent.CONFIG.obsidian_enabled = True
        mac_agent.CONFIG.obsidian_vault_dir = self.root / "vault"
        state = {}

        with (
            mock.patch.object(
                mac_agent,
                "_fetch_json",
                return_value={
                    "files": [
                        {
                            "path": "../outside.md",
                            "sha256": hashlib.sha256(b"incoming").hexdigest(),
                            "size": 8,
                            "mtime": 123.0,
                        }
                    ]
                },
            ),
            mock.patch.object(mac_agent, "_fetch_bytes") as fetch_bytes_mock,
            mock.patch.object(mac_agent, "_post_vault_file") as post_mock,
        ):
            mac_agent._sync_obsidian_vault(state)

        fetch_bytes_mock.assert_not_called()
        post_mock.assert_not_called()
        self.assertFalse((self.root / "outside.md").exists())
        self.assertEqual(state.get("vault"), {})


if __name__ == "__main__":
    unittest.main()
