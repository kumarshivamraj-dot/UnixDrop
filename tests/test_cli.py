from __future__ import annotations

import tempfile
import unittest
import os
from argparse import Namespace
from pathlib import Path
from unittest import mock

from unixdrop.cli import (
    _cmd_url,
    _cmd_deskflow,
    _drop_destination,
    _send_file_to_receiver,
    _stage_drop_files,
    _sync_dropwatch_once,
    _write_dropzone_upload,
)


class CliTests(unittest.TestCase):
    @mock.patch("unixdrop.cli.subprocess.run")
    def test_deskflow_client_allows_automatic_discovery(self, run_mock: mock.MagicMock) -> None:
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
        command = run_mock.call_args.args[0]
        self.assertNotIn("--server-ip", command)
        self.assertIn("--autostart", command)

    def test_stage_drop_files_copies_into_drop_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            drop_dir = root / "Drop to ThinkPad"
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
            drop_dir = Path(tmp) / "Drop to ThinkPad"

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
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{"ok": true, "path": "/inbox/report.txt"}'

            with mock.patch("unixdrop.cli.request.urlopen", return_value=response) as urlopen_mock:
                payload = _send_file_to_receiver(source, "http://receiver:8765/", "secret", 15)

            self.assertEqual(payload["path"], "/inbox/report.txt")
            req = urlopen_mock.call_args.args[0]
            self.assertEqual(req.full_url, "http://receiver:8765/api/file")
            self.assertEqual(req.headers["Authorization"], "Bearer secret")
            self.assertEqual(req.headers["X-filename"], "report.txt")
            self.assertEqual(req.data, b"hello")

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
        with (
            mock.patch("unixdrop.cli.load_config", return_value=cfg),
            mock.patch("unixdrop.cli.send_url") as send_mock,
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
