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
    _open_drop_folder_now,
    _parse_health,
    _quick_setup_deskflow,
    _restart_deskflow_client_now,
    _start_local_receiver_now,
    _start_linux_receiver_now,
    _swap_deskflow_role_now,
    _sync_receiver_endpoint,
    _update_quick_setup_config,
)


class TuiTests(unittest.TestCase):
    def test_quick_setup_config_enables_clipboard_and_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"auth_token": "token", "clipboard": {"mode": "off"}}))
            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                ok, detail = _update_quick_setup_config("client")

            self.assertTrue(ok, detail)
            payload = json.loads(config_path.read_text())
            self.assertEqual(payload["clipboard"]["mode"], "two_way")
            self.assertEqual(payload["deskflow"]["role"], "client")
            self.assertTrue(payload["deskflow"]["enabled"])

    @patch("unixdrop.tui._start_deskflow_now", return_value=(True, "started"))
    @patch("unixdrop.tui._update_quick_setup_config", return_value=(True, "saved"))
    @patch("unixdrop.tui._run_command", return_value=(True, "ok"))
    @patch("unixdrop.tui.sys.platform", "darwin")
    def test_quick_setup_mac_uses_peer_hostname(self, run_mock, _config_mock, _start_mock) -> None:
        ok, detail = _quick_setup_deskflow("thinkpad.local")

        self.assertTrue(ok, detail)
        command = run_mock.call_args.args[0]
        self.assertIn("server", command)
        self.assertIn("thinkpad.local", command)
        self.assertIn("right", command)

    @patch("unixdrop.tui._start_deskflow_now", return_value=(True, "started"))
    @patch("unixdrop.tui._update_quick_setup_config", return_value=(True, "saved"))
    @patch("unixdrop.tui._default_client_name", return_value="thinkpad")
    @patch("unixdrop.tui._run_command", return_value=(True, "ok"))
    @patch("unixdrop.tui.sys.platform", "linux")
    def test_quick_setup_linux_uses_discovery(self, run_mock, _name_mock, _config_mock, _start_mock) -> None:
        ok, detail = _quick_setup_deskflow()

        self.assertTrue(ok, detail)
        command = run_mock.call_args.args[0]
        self.assertIn("client", command)
        self.assertNotIn("--server-ip", command)

    def test_parse_health_rows(self) -> None:
        rows = _parse_health(
            [
                "Deskbridge health",
                "[ok] Peer HTTP receiver reachable: reachable",
                "[fail] send test ping: timeout",
            ]
        )
        self.assertEqual(
            rows,
            [
                (True, "Peer HTTP receiver reachable", "reachable"),
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
            load_config_mock.return_value = SimpleNamespace(deskflow_client_start_script=script)
            popen_mock.return_value = SimpleNamespace(pid=4321)

            ok, detail = _restart_deskflow_client_now()

            self.assertTrue(ok)
            self.assertIn("pid=4321", detail)
            popen_mock.assert_called_once_with([str(script)])
            self.assertEqual(run_mock.call_count, 2)

    @patch("unixdrop.tui.load_config")
    def test_restart_client_missing_script(self, load_config_mock) -> None:
        missing = Path("/tmp/does-not-exist-client.sh")
        load_config_mock.return_value = SimpleNamespace(deskflow_client_start_script=missing)
        ok, detail = _restart_deskflow_client_now()
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    @patch("unixdrop.tui.load_config")
    def test_restart_client_not_executable(self, load_config_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "start-deskflow-client.sh"
            script.write_text("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(script, 0o644)
            load_config_mock.return_value = SimpleNamespace(deskflow_client_start_script=script)
            ok, detail = _restart_deskflow_client_now()
            self.assertFalse(ok)
            self.assertIn("not executable", detail)

    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui.subprocess.run")
    @patch("unixdrop.tui.load_config")
    def test_swap_deskflow_role_server_to_client(self, load_config_mock, run_mock, popen_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server_script = Path(tmp) / "start-deskflow-server.sh"
            client_script = Path(tmp) / "start-deskflow-client.sh"
            server_script.write_text("#!/usr/bin/env bash\nexit 0\n")
            client_script.write_text("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(server_script, 0o755)
            os.chmod(client_script, 0o755)
            load_config_mock.return_value = SimpleNamespace(
                deskflow_mac_start_script=server_script,
                deskflow_linux_start_script=client_script,
            )
            popen_mock.return_value = SimpleNamespace(pid=2468)

            def run_side_effect(command, **_kwargs):
                pattern = command[-1]
                returncode = 0 if command[0] == "pgrep" and pattern == "deskflow-server" else 1
                return SimpleNamespace(returncode=returncode, stdout="", stderr="")

            run_mock.side_effect = run_side_effect

            ok, detail = _swap_deskflow_role_now()

            self.assertTrue(ok)
            self.assertIn("server -> client", detail)
            self.assertIn("pid=2468", detail)
            popen_mock.assert_called_once_with([str(client_script)])
            self.assertGreaterEqual(run_mock.call_count, 5)

    @patch("unixdrop.tui.sys.platform", "linux")
    @patch("unixdrop.tui.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr=""))
    @patch("unixdrop.tui.load_config")
    def test_swap_deskflow_role_reports_missing_opposite_script(self, load_config_mock, _run_mock) -> None:
        load_config_mock.return_value = SimpleNamespace(
            deskflow_mac_start_script=Path("/tmp/does-not-exist-server.sh"),
            deskflow_linux_start_script=Path("/tmp/does-not-exist-client.sh"),
        )

        ok, detail = _swap_deskflow_role_now()

        self.assertFalse(ok)
        self.assertIn("server start script missing", detail)

    @patch("unixdrop.tui.sys.platform", "linux")
    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui.subprocess.run")
    @patch("unixdrop.tui.load_config")
    def test_swap_deskflow_role_aborts_when_stop_fails(self, load_config_mock, run_mock, popen_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server_script = Path(tmp) / "start-deskflow-server.sh"
            server_script.write_text("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(server_script, 0o755)
            load_config_mock.return_value = SimpleNamespace(
                deskflow_mac_start_script=server_script,
                deskflow_linux_start_script=Path(tmp) / "start-deskflow-client.sh",
            )

            def run_side_effect(command, **_kwargs):
                if command[0] == "pgrep":
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                return SimpleNamespace(returncode=2, stdout="", stderr="permission denied")

            run_mock.side_effect = run_side_effect

            ok, detail = _swap_deskflow_role_now()

            self.assertFalse(ok)
            self.assertIn("failed to stop Deskflow process", detail)
            popen_mock.assert_not_called()

    @patch("unixdrop.tui.sys.platform", "linux")
    @patch("unixdrop.tui.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr=""))
    @patch("unixdrop.tui.load_config", side_effect=FileNotFoundError("missing config"))
    def test_swap_deskflow_role_reports_config_load_failure(self, _load_config_mock, _run_mock) -> None:
        ok, detail = _swap_deskflow_role_now()

        self.assertFalse(ok)
        self.assertIn("failed to load Deskflow config", detail)

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

    @patch("unixdrop.tui._local_tcp_open", return_value=True)
    @patch("unixdrop.tui.load_config")
    def test_start_local_receiver_noop_when_listening(self, load_config_mock, _local_open_mock) -> None:
        load_config_mock.return_value = SimpleNamespace(port=8765)
        ok, detail = _start_local_receiver_now()
        self.assertTrue(ok)
        self.assertIn("already listening", detail)

    @patch("unixdrop.tui._local_tcp_open", return_value=True)
    @patch("unixdrop.tui.load_config")
    def test_start_linux_receiver_alias_noop_when_listening(self, load_config_mock, _local_open_mock) -> None:
        load_config_mock.return_value = SimpleNamespace(port=8765)
        ok, detail = _start_linux_receiver_now()
        self.assertTrue(ok)
        self.assertIn("already listening", detail)

    @patch("unixdrop.tui.sys.platform", "darwin")
    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui.load_config")
    def test_open_drop_folder_uses_configured_folder(self, load_config_mock, popen_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            drop_dir = Path(tmp) / "Drop to ThinkPad"
            load_config_mock.return_value = SimpleNamespace(drop_dir=drop_dir)
            popen_mock.return_value = SimpleNamespace(pid=1234)

            ok, detail = _open_drop_folder_now()

            self.assertTrue(ok)
            self.assertIn(str(drop_dir), detail)
            popen_mock.assert_called_once()
            self.assertEqual(popen_mock.call_args.args[0], ["open", str(drop_dir)])
            self.assertTrue(drop_dir.exists())


if __name__ == "__main__":
    unittest.main()
