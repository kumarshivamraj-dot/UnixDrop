from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class DiagnosticsTests(unittest.TestCase):
    def test_health_report_handles_missing_config(self) -> None:
        from unixdrop.health import health_report

        with tempfile.TemporaryDirectory() as tmp:
            report = health_report(config_path=Path(tmp) / "missing.json")

        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][0]["name"], "Config load")

    def test_health_report_json_shape(self) -> None:
        from unixdrop.config import AppConfig
        from unixdrop.health import health_report

        cfg = AppConfig(
            auth_token="token",
            receiver_url="http://peer:8765",
            drop_dir=Path("/tmp/drop"),
            clipboard_mode="off",
        )

        with (
            mock.patch("unixdrop.health._request_json_timed", side_effect=OSError("offline")),
            mock.patch("unixdrop.health._check_launchd", return_value=(True, "not available")),
            mock.patch("unixdrop.health._check_systemd_local", return_value=(True, "not available")),
        ):
            report = health_report(config=cfg)

        self.assertIn("ok", report)
        self.assertTrue(any(check["name"] == "Peer HTTP receiver reachable" for check in report["checks"]))

    def test_health_clipboard_check_does_not_post_to_real_clipboard_endpoint(self) -> None:
        from unixdrop.config import AppConfig
        from unixdrop.health import health_report

        with tempfile.TemporaryDirectory() as tmp:
            drop_dir = Path(tmp) / "drop"
            drop_dir.mkdir()
            cfg = AppConfig(
                auth_token="token",
                receiver_url="http://peer:8765",
                drop_dir=drop_dir,
                clipboard_mode="two_way",
            )
            calls = []

            def fake_request_json(_cfg, path, method="GET", payload=None, auth=True):
                calls.append((path, method, payload, auth))
                if path == "/api/ping":
                    return {"ok": True, "pong": True}
                if path == "/api/health/clipboard-check":
                    text = payload["text"]
                    return {
                        "ok": True,
                        "stored": False,
                        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    }
                if path == "/api/clipboard":
                    return {"ok": True, "text": "user clipboard"}
                if path == "/api/health/write-check":
                    return {"ok": True}
                if path == "/api/diagnostics":
                    return {"link_opener_available": True, "link_opener": "xdg-open"}
                raise AssertionError(f"unexpected path: {path}")

            with (
                mock.patch("unixdrop.health._request_json_timed", return_value=({"ok": True}, 1.0)),
                mock.patch("unixdrop.health._request_json", side_effect=fake_request_json),
                mock.patch("unixdrop.health._check_launchd", return_value=(True, "not available")),
                mock.patch("unixdrop.health._check_systemd_local", return_value=(True, "active")),
            ):
                report = health_report(config=cfg)

        self.assertTrue(report["ok"])
        self.assertIn(("/api/health/clipboard-check", "POST"), [(path, method) for path, method, *_ in calls])
        self.assertNotIn(("/api/clipboard", "POST"), [(path, method) for path, method, *_ in calls])

    def test_status_report_fails_when_peer_unreachable(self) -> None:
        from unixdrop import status

        with mock.patch.object(
            status,
            "status_lines",
            return_value=[
                "Deskbridge status",
                "Local node service running: yes (systemd unixdrop-receiver.service: active)",
                "Peer receiver reachable: no ([Errno 111] Connection refused)",
            ],
        ):
            report = status.status_report()

        self.assertFalse(report["ok"])

    def test_status_report_ok_when_local_and_peer_are_up(self) -> None:
        from unixdrop import status

        with mock.patch.object(
            status,
            "status_lines",
            return_value=[
                "Deskbridge status",
                "Local node service running: yes (systemd unixdrop-receiver.service: active)",
                "Peer receiver reachable: yes (reachable)",
            ],
        ):
            report = status.status_report()

        self.assertTrue(report["ok"])

    def test_status_lines_include_active_config_and_receiver_url(self) -> None:
        from unixdrop.config import AppConfig
        from unixdrop.status import status_lines

        cfg = AppConfig(
            auth_token="token",
            receiver_url="http://peer:8765",
            drop_dir=Path("/tmp/drop"),
            inbox_dir=Path("/tmp/inbox"),
            clipboard_mode="off",
        )

        with (
            mock.patch("unixdrop.status._check_health", return_value=(False, {}, "offline", None)),
            mock.patch("unixdrop.status._check_local_node_service", return_value=(True, "active")),
            mock.patch("unixdrop.status._pending_drop_files", return_value=0),
            mock.patch("unixdrop.status._read_state", return_value={}),
            mock.patch("unixdrop.status._vault_status", return_value=["obsidian sync enabled: false"]),
        ):
            lines = status_lines(config=cfg, config_path=Path("/tmp/unixdrop-config.json"))

        self.assertIn("config file: /tmp/unixdrop-config.json", lines)
        self.assertIn("peer receiver URL: http://peer:8765", lines)

    def test_doctor_report_json_shape(self) -> None:
        from unixdrop.doctor import doctor_report

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"auth_token": "token", "receiver_url": "http://127.0.0.1:8765"}),
                encoding="utf-8",
            )

            with (
                mock.patch("unixdrop.doctor.service_manager_status", return_value=(True, "systemctl --user available")),
                mock.patch("unixdrop.doctor.link_opener_status", return_value=(True, "xdg-open available")),
                mock.patch("unixdrop.doctor.clipboard_tools_status", return_value=(True, "read/write available")),
                mock.patch("unixdrop.doctor.deskflow_binary_status", return_value=(True, "deskflow available")),
            ):
                report = doctor_report(config_path, platform="linux")

        self.assertTrue(report["ok"])
        self.assertTrue(all({"status", "name", "detail"} <= set(check) for check in report["checks"]))

    def test_browser_helper_import_does_not_load_config(self) -> None:
        with mock.patch.dict(os.environ, {"UNIXDROP_CONFIG": "/tmp/does-not-exist-unixdrop.json"}):
            module = importlib.import_module("unixdrop.send_browser_url")
            importlib.reload(module)

        self.assertTrue(module.is_supported_web_url("https://example.com"))


if __name__ == "__main__":
    unittest.main()
