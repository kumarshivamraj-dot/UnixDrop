from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unixdrop.tui import _parse_health, _restart_deskflow_client_now


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


if __name__ == "__main__":
    unittest.main()
