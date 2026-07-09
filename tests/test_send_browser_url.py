from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest import mock


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


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
        self.assertIn("could not reach peer receiver", str(exc.exception))
        self.assertIn("Connection refused", str(exc.exception))

    def test_firefox_alias_resolves(self) -> None:
        self.assertEqual(self.module._resolve_browser_arg("firefox"), "Firefox")
        self.assertEqual(self.module._resolve_browser_arg("firefox-developer"), "Firefox Developer Edition")
        self.assertEqual(self.module._resolve_browser_arg("librewolf"), "LibreWolf")

    def test_firefox_targets_single_web_tab_succeeds(self) -> None:
        url = self.module.firefox_url_from_targets(
            [
                {"type": "page", "url": "about:debugging"},
                {"type": "page", "url": "https://example.com/report"},
            ]
        )

        self.assertEqual(url, "https://example.com/report")

    def test_firefox_targets_active_marker_wins(self) -> None:
        url = self.module.firefox_url_from_targets(
            [
                {"type": "page", "url": "https://example.com/one"},
                {"type": "page", "url": "https://example.com/two", "active": True},
            ]
        )

        self.assertEqual(url, "https://example.com/two")

    def test_firefox_targets_nested_active_marker_wins(self) -> None:
        url = self.module.firefox_url_from_targets(
            [
                {"type": "page", "url": "https://example.com/one"},
                {
                    "type": "page",
                    "url": "https://example.com/two",
                    "targetInfo": {"focused": True},
                },
            ]
        )

        self.assertEqual(url, "https://example.com/two")

    def test_firefox_targets_ambiguous_tabs_fail(self) -> None:
        with self.assertRaises(SystemExit) as exc:
            self.module.firefox_url_from_targets(
                [
                    {"type": "page", "url": "https://example.com/one"},
                    {"type": "page", "url": "https://example.com/two"},
                ]
            )

        self.assertIn("multiple web tabs", str(exc.exception))

    def test_current_browser_context_reads_selected_firefox_endpoint(self) -> None:
        targets = [{"type": "page", "url": "https://example.com/from-firefox"}]
        with mock.patch.object(self.module.request, "urlopen", return_value=FakeResponse(targets)) as urlopen_mock:
            app_name, url = self.module.current_browser_context(
                "firefox",
                firefox_debug_url="http://127.0.0.1:9333",
            )

        self.assertEqual(app_name, "Firefox")
        self.assertEqual(url, "https://example.com/from-firefox")
        req_url = urlopen_mock.call_args.args[0]
        self.assertEqual(req_url, "http://127.0.0.1:9333/json/list")

    def test_firefox_endpoint_unreachable_reports_setup_guidance(self) -> None:
        with mock.patch.object(self.module.request, "urlopen", side_effect=URLError("Connection refused")):
            with self.assertRaises(SystemExit) as exc:
                self.module.current_browser_context("firefox")

        self.assertIn("could not reach Firefox debug endpoint", str(exc.exception))
        self.assertIn("--firefox-debug-url", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
