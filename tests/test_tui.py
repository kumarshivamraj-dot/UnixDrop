from __future__ import annotations

import os
import sys
import tempfile
import unittest
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unixdrop.config import ENV_CONFIG_PATH
from unixdrop.tui import (
    _apply_client_server_hosts,
    _apply_deskflow_endpoints,
    _bootstrap_tui_config,
    _collect_tui_snapshot,
    _configure_startup_deskflow,
    _current_deskflow_role,
    _deskflow_endpoint_from_host,
    _drop_panel_lines,
    _first_endpoint_host,
    _open_drop_folder_now,
    _parse_health,
    _parse_latency_ms,
    _quick_setup_deskflow,
    _restart_deskflow_client_now,
    _restart_deskflow_role_now,
    _receiver_url_from_host,
    _start_deskflow_now,
    _start_local_receiver_now,
    _start_linux_receiver_now,
    _startup_setup_needed,
    _stop_all_now,
    _swap_deskflow_role_now,
    _top_summary,
    _sync_receiver_endpoint,
    _update_quick_setup_config,
)


class TuiTests(unittest.TestCase):
    @patch("unixdrop.tui.subprocess.run")
    @patch("unixdrop.tui.sys.platform", "darwin")
    @patch("unixdrop.tui._set_deskflow_off", return_value=(True, "off"))
    def test_stop_all_unloads_services_and_kills_components(self, _off_mock, run_mock) -> None:
        run_mock.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            launch_agents = Path(tmp) / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            for name in (
                "com.unixdrop.agent.plist",
                "com.unixdrop.deskflow.server.plist",
                "com.unixdrop.deskflow.client.plist",
            ):
                (launch_agents / name).write_text("plist")
            with patch.dict(os.environ, {"HOME": tmp}):
                ok, detail = _stop_all_now()

        self.assertTrue(ok, detail)
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertTrue(any(command[:2] == ["launchctl", "unload"] for command in commands))
        self.assertTrue(any("unixdrop/discovery.py.*serve" in command for command in commands))
        self.assertTrue(any("-m unixdrop.node" in command for command in commands))

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

    def test_bootstrap_tui_config_creates_missing_config_and_runtime_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".config" / "unixdrop" / "config.json"
            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path), "HOME": str(root)}):
                ok, detail = _bootstrap_tui_config()

            self.assertTrue(ok, detail)
            self.assertTrue(config_path.exists())
            payload = json.loads(config_path.read_text())
            self.assertTrue(payload["auth_token"])
            self.assertTrue((root / "UnixDrop" / "Inbox").exists())
            self.assertTrue((root / "UnixDrop" / "Drop").exists())
            self.assertTrue((root / ".local" / "state" / "unixdrop").exists())

    @patch("unixdrop.tui.status_lines", side_effect=RuntimeError("status boom"))
    @patch("unixdrop.tui.health_lines", side_effect=RuntimeError("health boom"))
    def test_collect_tui_snapshot_logs_health_and_status_errors(self, _health_mock, _status_mock) -> None:
        events = deque(maxlen=10)

        checks, status = _collect_tui_snapshot(events)

        self.assertFalse(checks[0][0])
        self.assertIn("health boom", checks[0][2])
        self.assertEqual(status["peer receiver reachable"], "unknown")
        rendered_events = "\n".join(events)
        self.assertIn("health collection failed", rendered_events)
        self.assertIn("status collection failed", rendered_events)

    def test_deskflow_endpoint_from_plain_host_adds_port(self) -> None:
        self.assertEqual(_deskflow_endpoint_from_host("192.168.1.50"), "192.168.1.50:24800")
        self.assertEqual(_deskflow_endpoint_from_host("192.168.1.50:24801"), "192.168.1.50:24801")

    def test_receiver_url_from_host_ignores_deskflow_port(self) -> None:
        self.assertEqual(_receiver_url_from_host("192.168.1.50:24800"), "http://192.168.1.50:8765")
        self.assertEqual(_receiver_url_from_host("http://192.168.1.50:24800"), "http://192.168.1.50:8765")

    @patch("unixdrop.tui.sys.platform", "darwin")
    @patch("unixdrop.tui._start_deskflow_now", return_value=(True, "started"))
    @patch("unixdrop.tui._run_deskflow_setup", return_value=(True, "ok"))
    @patch("unixdrop.tui._stop_deskflow_processes", return_value=(True, "stopped"))
    @patch("unixdrop.tui._disable_standalone_deskflow_autostarts", return_value=(True, "disabled"))
    def test_startup_setup_mac_prompts_receiver_and_client_name(
        self, _disable_mock, _stop_mock, setup_mock, _start_mock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"auth_token": "token", "receiver_url": "http://127.0.0.1:8765"})
            )
            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                ok, detail = _configure_startup_deskflow(
                    peer_receiver="192.168.1.103",
                    client_name="thinkpad",
                )

            self.assertTrue(ok, detail)
            command = setup_mock.call_args.args[0]
            self.assertIn("--role", command)
            self.assertIn("server", command)
            self.assertIn("--client-name", command)
            self.assertIn("thinkpad", command)
            payload = json.loads(config_path.read_text())
            self.assertEqual(payload["receiver_url"], "http://192.168.1.103:8765")
            self.assertEqual(payload["deskflow"]["role"], "server")

    @patch("unixdrop.tui.sys.platform", "linux")
    @patch("unixdrop.tui._default_client_name", return_value="thinkpad")
    @patch("unixdrop.tui._start_deskflow_now", return_value=(True, "started"))
    @patch("unixdrop.tui._run_deskflow_setup", return_value=(True, "ok"))
    @patch("unixdrop.tui._stop_deskflow_processes", return_value=(True, "stopped"))
    @patch("unixdrop.tui._disable_standalone_deskflow_autostarts", return_value=(True, "disabled"))
    def test_startup_setup_linux_uses_server_ip_for_deskflow_and_receiver(
        self, _disable_mock, _stop_mock, setup_mock, _start_mock, _name_mock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"auth_token": "token", "receiver_url": "http://127.0.0.1:8765"})
            )
            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                ok, detail = _configure_startup_deskflow(server_host="192.168.1.50")

            self.assertTrue(ok, detail)
            command = setup_mock.call_args.args[0]
            self.assertIn("--role", command)
            self.assertIn("client", command)
            self.assertIn("--server-hosts", command)
            self.assertIn("192.168.1.50:24800", command)
            payload = json.loads(config_path.read_text())
            self.assertEqual(payload["receiver_url"], "http://192.168.1.50:8765")
            self.assertEqual(payload["deskflow"]["role"], "client")

    @patch("unixdrop.tui.sys.platform", "linux")
    @patch("unixdrop.tui._default_client_name", return_value="thinkpad")
    @patch("unixdrop.tui._start_deskflow_now", return_value=(True, "started"))
    @patch("unixdrop.tui._run_deskflow_setup", return_value=(True, "ok"))
    @patch("unixdrop.tui._stop_deskflow_processes", return_value=(True, "stopped"))
    @patch("unixdrop.tui._disable_standalone_deskflow_autostarts", return_value=(True, "disabled"))
    def test_startup_setup_linux_does_not_copy_deskflow_port_to_receiver(
        self, _disable_mock, _stop_mock, _setup_mock, _start_mock, _name_mock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"auth_token": "token", "receiver_url": "http://127.0.0.1:8765"})
            )
            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                ok, detail = _configure_startup_deskflow(server_host="192.168.1.50:24800")

            self.assertTrue(ok, detail)
            payload = json.loads(config_path.read_text())
            self.assertEqual(payload["receiver_url"], "http://192.168.1.50:8765")

    @patch("unixdrop.tui.sys.platform", "darwin")
    def test_startup_setup_not_needed_when_config_is_complete_on_mac(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "start-deskflow-server.sh"
            script.write_text("#!/usr/bin/env bash\nsleep 10\n")
            os.chmod(script, 0o755)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "auth_token": "token",
                        "receiver_url": "http://192.168.1.103:8765",
                        "deskflow": {
                            "enabled": True,
                            "role": "server",
                            "peer_name": "thinkpad",
                            "server_start_script": str(script),
                        },
                    }
                )
            )

            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                needed, detail = _startup_setup_needed()

        self.assertFalse(needed)
        self.assertIn("already complete", detail)

    @patch("unixdrop.tui.sys.platform", "linux")
    def test_startup_setup_needed_when_peer_receiver_is_still_localhost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "start-deskflow-client.sh"
            script.write_text("#!/usr/bin/env bash\nsleep 10\n")
            os.chmod(script, 0o755)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "auth_token": "token",
                        "receiver_url": "http://127.0.0.1:8765",
                        "deskflow": {
                            "enabled": True,
                            "role": "client",
                            "client_start_script": str(script),
                        },
                    }
                )
            )

            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                needed, detail = _startup_setup_needed()

        self.assertTrue(needed)
        self.assertIn("peer UnixDrop IP/host", detail)

    @patch("unixdrop.tui.sys.platform", "darwin")
    @patch("unixdrop.tui._current_deskflow_role", return_value=None)
    @patch("unixdrop.tui.subprocess.Popen")
    def test_start_deskflow_recovers_when_role_is_off(self, popen_mock, _role_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "start-deskflow-server.sh"
            script.write_text("#!/usr/bin/env bash\nsleep 10\n")
            os.chmod(script, 0o755)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "auth_token": "token",
                        "receiver_url": "http://127.0.0.1:8765",
                        "state_dir": str(root / "state"),
                        "deskflow": {
                            "enabled": False,
                            "role": "off",
                            "server_start_script": str(script),
                            "client_start_script": str(root / "start-deskflow-client.sh"),
                        },
                    }
                )
            )
            popen_mock.return_value = SimpleNamespace(pid=1234, poll=lambda: None)

            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                ok, detail = _start_deskflow_now()

            self.assertTrue(ok, detail)
            self.assertIn("deskflow server start requested", detail)
            popen_mock.assert_called_once()
            self.assertEqual(popen_mock.call_args.args[0], [str(script)])
            payload = json.loads(config_path.read_text())
            self.assertTrue(payload["deskflow"]["enabled"])
            self.assertEqual(payload["deskflow"]["role"], "server")

    @patch("unixdrop.tui.subprocess.run")
    def test_current_role_does_not_guess_plain_deskflow_core_role(self, run_mock) -> None:
        def run_side_effect(command, **_kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        run_mock.side_effect = run_side_effect

        self.assertIsNone(_current_deskflow_role())

    @patch("unixdrop.tui.subprocess.run")
    def test_current_role_detects_explicit_deskflow_core_server(self, run_mock) -> None:
        def run_side_effect(command, **_kwargs):
            pattern = command[-1]
            returncode = 0 if pattern == "deskflow-core.*server" else 1
            return SimpleNamespace(returncode=returncode, stdout="", stderr="")

        run_mock.side_effect = run_side_effect

        self.assertEqual(_current_deskflow_role(), "server")

    @patch("unixdrop.tui._stop_deskflow_processes", return_value=(True, "stopped"))
    @patch("unixdrop.tui._disable_standalone_deskflow_autostarts", return_value=(True, "disabled"))
    @patch("unixdrop.tui._start_deskflow_now", return_value=(True, "started"))
    @patch("unixdrop.tui._update_quick_setup_config", return_value=(True, "saved"))
    @patch("unixdrop.tui._run_deskflow_setup", return_value=(True, "ok"))
    @patch("unixdrop.tui.sys.platform", "darwin")
    def test_quick_setup_mac_uses_peer_hostname(
        self, setup_mock, _config_mock, _start_mock, _disable_mock, _stop_mock
    ) -> None:
        ok, detail = _quick_setup_deskflow("thinkpad.local")

        self.assertTrue(ok, detail)
        command = setup_mock.call_args.args[0]
        self.assertIn("server", command)
        self.assertIn("thinkpad.local", command)
        self.assertIn("right", command)

    @patch("unixdrop.tui._stop_deskflow_processes", return_value=(True, "stopped"))
    @patch("unixdrop.tui._disable_standalone_deskflow_autostarts", return_value=(True, "disabled"))
    @patch("unixdrop.tui._start_deskflow_now", return_value=(True, "started"))
    @patch("unixdrop.tui._update_quick_setup_config", return_value=(True, "saved"))
    @patch("unixdrop.tui._default_client_name", return_value="thinkpad")
    @patch("unixdrop.tui._run_deskflow_setup", return_value=(True, "ok"))
    @patch("unixdrop.tui.sys.platform", "linux")
    def test_quick_setup_linux_uses_discovery(
        self, setup_mock, _name_mock, _config_mock, _start_mock, _disable_mock, _stop_mock
    ) -> None:
        ok, detail = _quick_setup_deskflow()

        self.assertTrue(ok, detail)
        command = setup_mock.call_args.args[0]
        self.assertIn("client", command)
        self.assertNotIn("--server-ip", command)
        self.assertNotIn("--server-hosts", command)

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

    def test_parse_latency_ms(self) -> None:
        self.assertEqual(_parse_latency_ms("12 ms"), 12.0)
        self.assertEqual(_parse_latency_ms("7.5 ms"), 7.5)
        self.assertIsNone(_parse_latency_ms("unknown"))

    def test_top_summary_includes_latency_and_peer(self) -> None:
        summary = _top_summary(
            {
                "peer receiver reachable": "yes",
                "peer receiver latency": "14 ms",
                "clipboard_mode": "two_way",
                "deskflow_enabled": "yes",
                "deskflow_role": "client",
                "peer hostname": "kashira",
            },
            [14.0, 12.0, 16.0],
        )
        self.assertIn("receiver up", summary)
        self.assertIn("latency 14 ms", summary)
        self.assertIn("jitter", summary)
        self.assertIn("peer kashira", summary)

    def test_drop_panel_uses_clear_labels_and_queue_state(self) -> None:
        lines = _drop_panel_lines(
            {
                "local drop folder": "/home/snape/UnixDrop/outbox",
                "local inbox": "/home/snape/UnixDrop/inbox",
                "pending files in drop folder": "0",
                "last upload result": "none",
            },
            width=64,
        )

        self.assertIn("drop to peer", lines[0])
        self.assertTrue(any("drop folder" in line for line in lines))
        self.assertTrue(any("local inbox" in line for line in lines))
        self.assertTrue(any("idle - no files waiting" in line for line in lines))
        self.assertTrue(all(len(line) == 64 for line in lines))

    def test_drop_panel_truncates_long_paths(self) -> None:
        lines = _drop_panel_lines(
            {
                "local drop folder": "/home/snape/very/long/path/that/keeps/going/outbox",
                "local inbox": "/home/snape/UnixDrop/inbox",
                "pending files in drop folder": "12",
                "last upload result": "uploaded huge-report.pdf",
            },
            width=56,
        )

        rendered = "\n".join(lines)
        self.assertIn("...", rendered)
        self.assertIn("12 files waiting", rendered)
        self.assertTrue(all(len(line) == 56 for line in lines))

    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui._stop_deskflow_processes", return_value=(True, "stopped"))
    @patch("unixdrop.tui.load_config")
    def test_restart_client_uses_client_script(self, load_config_mock, stop_mock, popen_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "start-deskflow-client.sh"
            script.write_text("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(script, 0o755)
            load_config_mock.return_value = SimpleNamespace(deskflow_client_start_script=script, state_dir=Path(tmp))
            popen_mock.return_value = SimpleNamespace(pid=4321, poll=lambda: None)

            ok, detail = _restart_deskflow_client_now()

        self.assertTrue(ok)
        self.assertIn("pid=4321", detail)
        popen_mock.assert_called_once()
        self.assertEqual(popen_mock.call_args.args[0], [str(script)])
        stop_mock.assert_called_once()

    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui._stop_deskflow_processes", return_value=(True, "stopped"))
    @patch("unixdrop.tui.load_config")
    def test_restart_role_reports_immediate_exit(self, load_config_mock, _stop_mock, popen_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "start-deskflow-server.sh"
            script.write_text("#!/usr/bin/env bash\nexit 9\n")
            os.chmod(script, 0o755)
            load_config_mock.return_value = SimpleNamespace(deskflow_server_start_script=script, state_dir=Path(tmp))
            popen_mock.return_value = SimpleNamespace(pid=4321, poll=lambda: 9)

            ok, detail = _restart_deskflow_role_now("server")

        self.assertFalse(ok)
        self.assertIn("exited with code 9", detail)

    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui._stop_deskflow_processes", return_value=(False, "stop failed"))
    @patch("unixdrop.tui.load_config")
    def test_restart_role_aborts_when_stop_fails(self, load_config_mock, _stop_mock, popen_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "start-deskflow-server.sh"
            script.write_text("#!/usr/bin/env bash\nsleep 10\n")
            os.chmod(script, 0o755)
            load_config_mock.return_value = SimpleNamespace(deskflow_server_start_script=script, state_dir=Path(tmp))

            ok, detail = _restart_deskflow_role_now("server")

        self.assertFalse(ok)
        self.assertIn("stop failed", detail)
        popen_mock.assert_not_called()

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
    @patch("unixdrop.tui._current_deskflow_role", side_effect=["server", None])
    @patch("unixdrop.tui.load_config")
    def test_swap_deskflow_role_server_to_client(
        self, load_config_mock, current_role_mock, run_mock, popen_mock
    ) -> None:
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
                state_dir=Path(tmp),
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
            popen_mock.assert_called_once()
            self.assertEqual(popen_mock.call_args.args[0], [str(client_script)])
            self.assertGreaterEqual(run_mock.call_count, 4)

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
                        "receiver_url": "http://203.0.113.70:8765",
                        "receiver": {
                            "host": "203.0.113.70",
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
                    "http://198.51.100.70:8765",
                )
            self.assertTrue(ok)
            self.assertIn("http://198.51.100.70:8765", detail)
            payload = json.loads(config_path.read_text())
            self.assertEqual(payload["receiver"]["host"], "198.51.100.70")
            self.assertEqual(payload["receiver_url"], "http://198.51.100.70:8765")

    def test_sync_receiver_endpoint_blank_leaves_config_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            original = {
                "auth_token": "token",
                "receiver_url": "http://203.0.113.20:8765",
                "receiver": {
                    "host": "203.0.113.20",
                    "port": 8765,
                },
            }
            config_path.write_text(json.dumps(original))
            with patch.dict(os.environ, {ENV_CONFIG_PATH: str(config_path)}):
                ok, detail = _sync_receiver_endpoint("", None)
            self.assertTrue(ok)
            self.assertIn("unchanged", detail)
            self.assertEqual(json.loads(config_path.read_text()), original)

    @patch("unixdrop.tui._default_client_name", return_value="peer-laptop")
    @patch("unixdrop.tui._run_deskflow_setup", return_value=(True, "ok"))
    def test_apply_client_server_hosts_blank_uses_discovery(self, setup_mock, _name_mock) -> None:
        ok, detail = _apply_client_server_hosts("")

        self.assertTrue(ok, detail)
        command = setup_mock.call_args.args[0]
        self.assertIn("--role", command)
        self.assertIn("client", command)
        self.assertIn("--autostart", command)
        self.assertNotIn("--server-hosts", command)
        self.assertIn("LAN discovery", detail)

    @patch("unixdrop.tui._run_deskflow_setup", return_value=(True, "ok"))
    def test_apply_deskflow_endpoints_server_uses_server_role(self, setup_mock) -> None:
        ok, detail = _apply_deskflow_endpoints(role="server", client_name="thinkpad")

        self.assertTrue(ok, detail)
        command = setup_mock.call_args.args[0]
        self.assertIn("--role", command)
        self.assertIn("server", command)
        self.assertIn("--client-name", command)
        self.assertIn("thinkpad", command)
        self.assertNotIn("client", command[command.index("--role") + 1])

    @patch("unixdrop.tui._default_client_name", return_value="peer-laptop")
    @patch("unixdrop.tui._run_deskflow_setup", return_value=(True, "ok"))
    def test_apply_deskflow_endpoints_client_uses_client_role(self, setup_mock, _name_mock) -> None:
        ok, detail = _apply_deskflow_endpoints(role="client", server_hosts="192.168.1.10:24800")

        self.assertTrue(ok, detail)
        command = setup_mock.call_args.args[0]
        self.assertIn("--role", command)
        self.assertIn("client", command)
        self.assertIn("--server-hosts", command)
        self.assertIn("192.168.1.10:24800", command)

    @patch("unixdrop.tui._local_tcp_open", return_value=True)
    @patch("unixdrop.tui.load_config")
    def test_start_local_receiver_noop_when_listening(self, load_config_mock, _local_open_mock) -> None:
        load_config_mock.return_value = SimpleNamespace(port=8765, state_dir=Path(tempfile.gettempdir()))
        ok, detail = _start_local_receiver_now()
        self.assertTrue(ok)
        self.assertIn("already listening", detail)

    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui._local_tcp_open", side_effect=[False, True])
    @patch("unixdrop.tui.load_config")
    def test_start_local_receiver_uses_installed_module_path(
        self, load_config_mock, _local_open_mock, popen_mock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            load_config_mock.return_value = SimpleNamespace(port=8765, state_dir=Path(tmp))
            popen_mock.return_value = SimpleNamespace(pid=1234, poll=lambda: None)

            ok, detail = _start_local_receiver_now()

        self.assertTrue(ok, detail)
        self.assertIn("pid=1234", detail)
        popen_mock.assert_called_once()
        self.assertEqual(popen_mock.call_args.args[0], [sys.executable, "-m", "unixdrop.linux_service"])
        self.assertNotIn("cwd", popen_mock.call_args.kwargs)
        self.assertNotIn("env", popen_mock.call_args.kwargs)

    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui._tail_file", return_value="receiver boom")
    @patch("unixdrop.tui._local_tcp_open", return_value=False)
    @patch("unixdrop.tui.load_config")
    def test_start_local_receiver_reports_immediate_exit(
        self, load_config_mock, _local_open_mock, _tail_mock, popen_mock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            load_config_mock.return_value = SimpleNamespace(port=8765, state_dir=Path(tmp))
            popen_mock.return_value = SimpleNamespace(pid=1234, poll=lambda: 2)

            ok, detail = _start_local_receiver_now()

        self.assertFalse(ok)
        self.assertIn("exited with code 2", detail)
        self.assertIn("receiver boom", detail)

    @patch("unixdrop.tui._local_tcp_open", return_value=True)
    @patch("unixdrop.tui.load_config")
    def test_start_linux_receiver_alias_noop_when_listening(self, load_config_mock, _local_open_mock) -> None:
        load_config_mock.return_value = SimpleNamespace(port=8765, state_dir=Path(tempfile.gettempdir()))
        ok, detail = _start_linux_receiver_now()
        self.assertTrue(ok)
        self.assertIn("already listening", detail)

    @patch("unixdrop.tui.sys.platform", "darwin")
    @patch("unixdrop.tui.subprocess.Popen")
    @patch("unixdrop.tui.load_config")
    def test_open_drop_folder_uses_configured_folder(self, load_config_mock, popen_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            drop_dir = Path(tmp) / "Drop to Peer"
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
