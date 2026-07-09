from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
