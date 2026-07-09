from __future__ import annotations

import tempfile
import unittest
import json
import os
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from contextlib import redirect_stdout
import io

from unixdrop.cli import (
    _cmd_up,
    _cmd_tab,
    _cmd_url,
    _cmd_deskflow,
    _cmd_init,
    _cmd_setup,
    _deskflow_command_args,
    _drop_destination,
    _send_file_to_receiver,
    _stage_drop_files,
    _sync_dropwatch_once,
    _write_dropzone_upload,
)


class CliTests(unittest.TestCase):
    @mock.patch("unixdrop.deskflow_setup.main", return_value=0)
    def test_deskflow_client_allows_automatic_discovery(self, setup_mock: mock.MagicMock) -> None:
        args = Namespace(
            role="client",
            server_ip=None,
            server_hosts=None,
            client_name="thinkpad",
            server_name=None,
            direction=None,
            config_dir=None,
            autostart=True,
            verify=False,
        )

        self.assertEqual(_cmd_deskflow(args), 0)
        command = setup_mock.call_args.args[0]
        self.assertNotIn("--server-ip", command)
        self.assertNotIn("--server-hosts", command)
        self.assertIn("--autostart", command)

    def test_deskflow_uses_packaged_python_setup(self) -> None:
        args = Namespace(
            role="client",
            server_ip=None,
            server_hosts=None,
            client_name="thinkpad",
            server_name=None,
            direction=None,
            config_dir=None,
            autostart=True,
            verify=False,
        )

        with mock.patch("unixdrop.deskflow_setup.main", return_value=0) as setup_mock:
            self.assertEqual(_cmd_deskflow(args), 0)

        setup_mock.assert_called_once_with(["--role", "client", "--client-name", "thinkpad", "--autostart"])

    @mock.patch("unixdrop.cli.sys.platform", "linux")
    @mock.patch("unixdrop.cli.subprocess.run")
    @mock.patch("unixdrop.service_install.install_linux_service", return_value=Path("/tmp/unixdrop-receiver.service"))
    @mock.patch("unixdrop.health.health_lines", return_value=["Deskbridge health", "[ok] sample: ok"])
    def test_cmd_up_prints_generated_service_path_and_python(
        self, _health_mock, install_mock, run_mock
    ) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            result = _cmd_up(Namespace())

        self.assertEqual(result, 0)
        self.assertIn("/tmp/unixdrop-receiver.service", output.getvalue())
        self.assertIn(sys.executable, output.getvalue())
        install_mock.assert_called_once()
        run_mock.assert_called_once_with(
            ["systemctl", "--user", "enable", "--now", "unixdrop-receiver.service"],
            check=True,
        )

    def test_deskflow_command_args_include_manual_fallbacks(self) -> None:
        args = Namespace(
            role="client",
            server_ip=None,
            server_hosts="192.168.1.50:24800,100.64.0.2:24800",
            client_name="thinkpad",
            server_name=None,
            direction=None,
            config_dir="/tmp/deskflow",
            autostart=False,
            verify=True,
        )

        command = _deskflow_command_args(args)

        self.assertIn("--server-hosts", command)
        self.assertIn("192.168.1.50:24800,100.64.0.2:24800", command)
        self.assertIn("--config-dir", command)
        self.assertIn("--verify", command)

    def test_cmd_init_creates_starter_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            args = Namespace(config=str(path), force=False)

            result = _cmd_init(args)

            self.assertEqual(result, 0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["auth_token"])
            self.assertEqual(payload["receiver"]["port"], 8765)
            self.assertEqual(payload["drop_dir"], "~/UnixDrop/Drop")

    def test_cmd_init_refuses_existing_config_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"auth_token": "keep"}\n', encoding="utf-8")
            args = Namespace(config=str(path), force=False)

            result = _cmd_init(args)

            self.assertEqual(result, 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["auth_token"], "keep")

    def test_cmd_setup_writes_peer_and_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            args = Namespace(
                config=str(path),
                force=False,
                auth_token="shared-token",
                peer_url="peer.local",
                discover=True,
                discovery_timeout=0.01,
                clipboard="two_way",
                role="client",
                client_name="thinkpad",
                direction="right",
                autostart=True,
            )
            output = io.StringIO()

            with (
                mock.patch("unixdrop.cli._probe_receiver", return_value=(True, "reachable")),
                redirect_stdout(output),
            ):
                result = _cmd_setup(args)

            self.assertEqual(result, 0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["auth_token"], "shared-token")
            self.assertEqual(payload["receiver_url"], "http://peer.local:8765")
            self.assertEqual(payload["clipboard"]["mode"], "two_way")
            self.assertEqual(payload["deskflow"]["role"], "client")
            self.assertIn("deskbridge up", output.getvalue())

    def test_cmd_setup_uses_discovered_peer_when_url_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            args = Namespace(
                config=str(path),
                force=False,
                auth_token=None,
                peer_url=None,
                discover=True,
                discovery_timeout=0.01,
                clipboard=None,
                role=None,
                client_name=None,
                direction="right",
                autostart=False,
            )

            with (
                mock.patch("unixdrop.cli._discover_receiver_url", return_value=("http://192.168.1.10:8765", "found")),
                mock.patch("unixdrop.cli._probe_receiver", return_value=(False, "offline")),
                redirect_stdout(io.StringIO()),
            ):
                result = _cmd_setup(args)

            self.assertEqual(result, 0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["receiver_url"], "http://192.168.1.10:8765")

    def test_cmd_setup_does_not_change_local_receiver_port_from_peer_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            args = Namespace(
                config=str(path),
                force=False,
                auth_token=None,
                peer_url="http://peer.local:9999",
                discover=False,
                discovery_timeout=0.01,
                clipboard=None,
                role=None,
                client_name=None,
                direction="right",
                autostart=False,
            )

            with (
                mock.patch("unixdrop.cli._probe_receiver", return_value=(True, "reachable")),
                redirect_stdout(io.StringIO()),
            ):
                result = _cmd_setup(args)

            self.assertEqual(result, 0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["receiver_url"], "http://peer.local:9999")
            self.assertEqual(payload["receiver"]["port"], 8765)

    def test_stage_drop_files_copies_into_drop_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            drop_dir = root / "Drop to Peer"
            source.write_text("hello", encoding="utf-8")

            staged = _stage_drop_files([str(source)], drop_dir)

            self.assertEqual(len(staged), 1)
            self.assertEqual(staged[0], drop_dir / "source.txt")
            self.assertEqual(staged[0].read_text(encoding="utf-8"), "hello")

    def test_drop_destination_uses_conflict_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            drop_dir = Path(tmp)
            source = drop_dir / "file.txt"
            source.write_text("existing", encoding="utf-8")

            destination = _drop_destination(drop_dir, source)

            self.assertNotEqual(destination, source)
            self.assertTrue(destination.name.startswith("file (drop "))
            self.assertEqual(destination.suffix, ".txt")

    def test_write_dropzone_upload_sanitizes_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            drop_dir = Path(tmp) / "Drop to Peer"

            destination = _write_dropzone_upload(drop_dir, "../report.txt", b"payload")

            self.assertEqual(destination, drop_dir / "report.txt")
            self.assertEqual(destination.read_bytes(), b"payload")

    def test_write_dropzone_upload_rejects_empty_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _write_dropzone_upload(Path(tmp), "", b"payload")

    def test_send_file_to_receiver_posts_file_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "report.txt"
            source.write_text("hello", encoding="utf-8")

            with mock.patch("unixdrop.cli.post_file", return_value={"ok": True, "path": "/inbox/report.txt"}) as post_mock:
                payload = _send_file_to_receiver(source, "http://receiver:8765/", "secret", 15)

            self.assertEqual(payload["path"], "/inbox/report.txt")
            post_mock.assert_called_once_with(
                url="http://receiver:8765/api/file",
                file_path=source,
                timeout_seconds=15,
                headers={
                    "Authorization": "Bearer secret",
                    "Content-Type": "application/octet-stream",
                    "X-Filename": "report.txt",
                },
            )

    def test_send_file_to_receiver_rejects_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _send_file_to_receiver(Path(tmp), "http://receiver:8765", "secret", 15)

    def test_cmd_url_sends_explicit_url(self) -> None:
        args = mock.Mock()
        args.url = "https://example.com"
        args.no_open = True
        args.to = "http://peer:8765"

        cfg = mock.Mock(auth_token="secret", receiver_url="http://configured:8765", request_timeout_seconds=15)
        send_mock = mock.Mock()
        helper = SimpleNamespace(is_supported_web_url=lambda url: True, send_url=send_mock)
        with (
            mock.patch("unixdrop.cli.load_config", return_value=cfg),
            mock.patch.dict(sys.modules, {"unixdrop.send_browser_url": helper}),
        ):
            result = _cmd_url(args)

        self.assertEqual(result, 0)
        send_mock.assert_called_once_with(
            "https://example.com",
            no_open=True,
            receiver_url="http://peer:8765",
            auth_token="secret",
            timeout_seconds=15,
            source="deskbridge-url",
        )

    def test_cmd_tab_passes_firefox_debug_url(self) -> None:
        args = Namespace(
            browser="firefox",
            no_open=True,
            firefox_debug_url="http://127.0.0.1:9333",
        )
        current_mock = mock.Mock(return_value=("Firefox", "https://example.com/firefox"))
        send_mock = mock.Mock()
        helper = SimpleNamespace(
            current_browser_context=current_mock,
            is_supported_web_url=lambda url: True,
            send_url=send_mock,
        )

        with mock.patch.dict(sys.modules, {"unixdrop.send_browser_url": helper}):
            result = _cmd_tab(args)

        self.assertEqual(result, 0)
        current_mock.assert_called_once_with("firefox", firefox_debug_url="http://127.0.0.1:9333")
        send_mock.assert_called_once_with(
            "https://example.com/firefox",
            no_open=True,
            source="mac-browser-helper",
        )

    def test_dropwatch_waits_for_stable_file_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "report.txt"
            source.write_text("hello", encoding="utf-8")
            state: dict = {}

            with mock.patch("unixdrop.cli._send_file_to_receiver") as send_mock:
                uploaded = _sync_dropwatch_once(
                    folder=folder,
                    receiver_url="http://receiver:8765",
                    auth_token="secret",
                    timeout_seconds=15,
                    max_file_mb=500,
                    delete_after_send=False,
                    state=state,
                )

            self.assertEqual(uploaded, 0)
            send_mock.assert_not_called()

    def test_dropwatch_uploads_stable_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "report.txt"
            source.write_text("hello", encoding="utf-8")
            old_mtime = source.stat().st_mtime - 2
            os.utime(source, (old_mtime, old_mtime))
            stat = source.stat()
            state = {"pending": {"report.txt": {"size": stat.st_size, "mtime": stat.st_mtime, "stable_checks": 0}}}

            with mock.patch("unixdrop.cli._send_file_to_receiver", return_value={"path": "/inbox/report.txt"}) as send_mock:
                uploaded = _sync_dropwatch_once(
                    folder=folder,
                    receiver_url="http://receiver:8765",
                    auth_token="secret",
                    timeout_seconds=15,
                    max_file_mb=500,
                    delete_after_send=False,
                    state=state,
                )

            self.assertEqual(uploaded, 1)
            send_mock.assert_called_once_with(source, "http://receiver:8765", "secret", 15)
            self.assertIn("report.txt", state["files"])


if __name__ == "__main__":
    unittest.main()
