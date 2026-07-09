from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unixdrop.doctor import doctor_checks, doctor_exit_code, doctor_lines


class DoctorTests(unittest.TestCase):
    def _write_config(self, payload: dict) -> Path:
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        try:
            json.dump(payload, handle)
            handle.flush()
            return Path(handle.name)
        finally:
            handle.close()

    @mock.patch("unixdrop.doctor.service_manager_status", return_value=(True, "systemctl --user available"))
    @mock.patch("unixdrop.doctor.link_opener_status", return_value=(True, "xdg-open available"))
    @mock.patch("unixdrop.doctor.clipboard_tools_status", return_value=(False, "no clipboard tools"))
    @mock.patch("unixdrop.doctor.deskflow_binary_status", return_value=(False, "deskflow not found"))
    def test_doctor_warns_for_optional_missing_tools(
        self,
        _deskflow_mock,
        _clipboard_mock,
        _opener_mock,
        _service_mock,
    ) -> None:
        config_path = self._write_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "clipboard": {"mode": "off"},
                "deskflow": {"role": "off"},
                "receiver": {"auto_open_links": False},
            }
        )

        checks = doctor_checks(config_path, platform="linux")

        self.assertEqual(doctor_exit_code(checks), 0)
        rendered = "\n".join(check.line() for check in checks)
        self.assertIn("[warn] Clipboard tools", rendered)
        self.assertIn("[warn] Deskflow binary", rendered)
        self.assertIn("[ok] Firefox debug endpoint: not configured", rendered)

    @mock.patch("unixdrop.doctor.service_manager_status", return_value=(True, "systemctl --user available"))
    @mock.patch("unixdrop.doctor.link_opener_status", return_value=(False, "xdg-open not found"))
    @mock.patch("unixdrop.doctor.clipboard_tools_status", return_value=(False, "no clipboard tools"))
    @mock.patch("unixdrop.doctor.deskflow_binary_status", return_value=(False, "deskflow not found"))
    def test_doctor_fails_for_required_configured_tools(
        self,
        _deskflow_mock,
        _clipboard_mock,
        _opener_mock,
        _service_mock,
    ) -> None:
        config_path = self._write_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "clipboard": {"mode": "two_way"},
                "deskflow": {"enabled": True, "role": "client"},
                "receiver": {"auto_open_links": True},
            }
        )

        checks = doctor_checks(config_path, platform="linux")

        self.assertEqual(doctor_exit_code(checks), 1)
        rendered = "\n".join(check.line() for check in checks)
        self.assertIn("[fail] Link opener", rendered)
        self.assertIn("[fail] Clipboard tools", rendered)
        self.assertIn("[fail] Deskflow binary", rendered)

    @mock.patch("unixdrop.doctor.service_manager_status", return_value=(False, "systemctl not found"))
    @mock.patch("unixdrop.doctor.link_opener_status", return_value=(False, "xdg-open not found"))
    @mock.patch("unixdrop.doctor.clipboard_tools_status", return_value=(False, "no clipboard tools"))
    @mock.patch("unixdrop.doctor.deskflow_binary_status", return_value=(False, "deskflow not found"))
    def test_doctor_reports_missing_config(
        self,
        _deskflow_mock,
        _clipboard_mock,
        _opener_mock,
        _service_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            lines = doctor_lines(missing, platform="linux")

        rendered = "\n".join(lines)
        self.assertIn("Deskbridge doctor", lines[0])
        self.assertIn("[fail] Config file: missing", rendered)
        self.assertIn("[fail] Service manager: systemctl not found", rendered)

    @mock.patch("unixdrop.doctor.service_manager_status", return_value=(True, "systemctl --user available"))
    @mock.patch("unixdrop.doctor.link_opener_status", return_value=(True, "xdg-open available"))
    @mock.patch("unixdrop.doctor.clipboard_tools_status", return_value=(True, "read: wl-paste, write: wl-copy"))
    @mock.patch("unixdrop.doctor.deskflow_binary_status", return_value=(True, "deskflow-client available"))
    @mock.patch("unixdrop.doctor.request.urlopen")
    def test_doctor_checks_firefox_when_configured(
        self,
        urlopen_mock,
        _deskflow_mock,
        _clipboard_mock,
        _opener_mock,
        _service_mock,
    ) -> None:
        config_path = self._write_config(
            {
                "auth_token": "token",
                "receiver_url": "http://127.0.0.1:8765",
                "tabs": {
                    "default_browser": "firefox",
                    "firefox_debug_url": "http://127.0.0.1:9333",
                },
            }
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"
        urlopen_mock.return_value = response

        checks = doctor_checks(config_path, platform="linux")

        self.assertEqual(doctor_exit_code(checks), 0)
        self.assertIn("[ok] Firefox debug endpoint", "\n".join(check.line() for check in checks))
        self.assertEqual(urlopen_mock.call_args.args[0], "http://127.0.0.1:9333/json/list")


if __name__ == "__main__":
    unittest.main()
