from __future__ import annotations

import os
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unixdrop.config import ENV_CONFIG_PATH
from unixdrop.tui import (
    _first_endpoint_host,
    _parse_health,
    _restart_deskflow_client_now,
    _sync_receiver_endpoint,
)


class TuiTests(unittest.TestCase):
    def test_parse_health_rows(self) -> None:
        rows = _parse_health(
            [
                "Deskbridge health",
                "[ok] HTTP receiver reachable: reachable",
                "[fail] send test ping: timeout",
            ]
        )
        self.assertEqual(
            rows,
            [
                (True, "HTTP receiver reachable", "reachable"),
                (False, "send test ping", "timeout"),
            ],
        )

    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui.subprocess.run")
    @patch("unixdrop.tui.load_config")
    def test_restart_client_uses_client_script(self, load_config_mock, run_mock, popen_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "start-deskflow-client.sh"
            script.write_text("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(script, 0o755)
            load_config_mock.return_value = SimpleNamespace(deskflow_linux_start_script=script)
            popen_mock.return_value = SimpleNamespace(pid=4321)

            ok, detail = _restart_deskflow_client_now()

            self.assertTrue(ok)
            self.assertIn("pid=4321", detail)
            popen_mock.assert_called_once_with([str(script)])
            self.assertEqual(run_mock.call_count, 2)

    @patch("unixdrop.tui.load_config")
    def test_restart_client_missing_script(self, load_config_mock) -> None:
        missing = Path("/tmp/does-not-exist-client.sh")
        load_config_mock.return_value = SimpleNamespace(deskflow_linux_start_script=missing)
        ok, detail = _restart_deskflow_client_now()
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    @patch("unixdrop.tui.load_config")
    def test_restart_client_not_executable(self, load_config_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "start-deskflow-client.sh"
            script.write_text("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(script, 0o644)
            load_config_mock.return_value = SimpleNamespace(deskflow_linux_start_script=script)
            ok, detail = _restart_deskflow_client_now()
            self.assertFalse(ok)
            self.assertIn("not executable", detail)

    def test_first_endpoint_host(self) -> None:
        self.assertEqual(_first_endpoint_host("192.168.1.5:24800,100.64.0.2:24800"), "192.168.1.5")
        self.assertEqual(_first_endpoint_host("  server.local:24800 "), "server.local")
        self.assertEqual(_first_endpoint_host(""), "")

    def test_sync_receiver_endpoint_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "auth_token": "token",
                        "receiver_url": "http://100.118.15.70:8765",
                        "receiver": {
                            "host": "100.118.15.70",
                            "port": 8765,
                        },
                    }
                )
            )
            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                ok, detail = _sync_receiver_endpoint("192.168.1.5:24800,100.64.0.2:24800")
            self.assertTrue(ok)
            self.assertIn("http://192.168.1.5:8765", detail)
            payload = json.loads(config_path.read_text())
            self.assertEqual(payload["receiver"]["host"], "192.168.1.5")
            self.assertEqual(payload["receiver_url"], "http://192.168.1.5:8765")

    def test_sync_receiver_endpoint_override_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "auth_token": "token",
                        "receiver_url": "http://10.0.0.8:8765",
                        "receiver": {
                            "host": "10.0.0.8",
                            "port": 8765,
                        },
                    }
                )
            )
            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                ok, detail = _sync_receiver_endpoint(
                    "192.168.1.5:24800",
                    "http://100.118.15.70:8765",
                )
            self.assertTrue(ok)
            self.assertIn("http://100.118.15.70:8765", detail)
            payload = json.loads(config_path.read_text())
            self.assertEqual(payload["receiver"]["host"], "100.118.15.70")
            self.assertEqual(payload["receiver_url"], "http://100.118.15.70:8765")


if __name__ == "__main__":
    unittest.main()
