from __future__ import annotations

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
