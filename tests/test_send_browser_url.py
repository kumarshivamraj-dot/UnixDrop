from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest import mock


class SendBrowserUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tempdir = tempfile.TemporaryDirectory()
        config_path = Path(cls._tempdir.name) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "auth_token": "token",
                    "receiver_url": "http://127.0.0.1:8765",
                }
            )
        )
        os.environ["UNIXDROP_CONFIG"] = str(config_path)
        cls.module = importlib.import_module("unixdrop.send_browser_url")
        cls.module = importlib.reload(cls.module)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tempdir.cleanup()

    def test_send_url_reports_http_error(self) -> None:
        http_error = HTTPError(
            url="http://127.0.0.1:8765/api/link",
            code=401,
            msg="unauthorized",
            hdrs=None,
            fp=None,
        )
        with mock.patch.object(self.module.request, "urlopen", side_effect=http_error):
            with self.assertRaises(SystemExit) as exc:
                self.module.send_url("https://example.com")
        self.assertIn("receiver rejected tab send (401)", str(exc.exception))

    def test_send_url_reports_unreachable_receiver(self) -> None:
        with mock.patch.object(self.module.request, "urlopen", side_effect=URLError("Connection refused")):
            with self.assertRaises(SystemExit) as exc:
                self.module.send_url("https://example.com")
        self.assertIn("could not reach Linux receiver", str(exc.exception))
        self.assertIn("Connection refused", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
