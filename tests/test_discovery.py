from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from unixdrop.discovery import _read_cache, _valid_reply, _write_cache


class DiscoveryTests(unittest.TestCase):
    def test_valid_reply(self) -> None:
        payload = _valid_reply(
            b'{"protocol":"unixdrop-discovery-v1","service":"deskflow","name":"mac","port":24800}'
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["port"], 24800)

    def test_rejects_wrong_service_and_invalid_port(self) -> None:
        self.assertIsNone(_valid_reply(b'{"protocol":"unixdrop-discovery-v1","service":"other","port":24800}'))
        self.assertIsNone(_valid_reply(b'{"protocol":"unixdrop-discovery-v1","service":"deskflow","port":70000}'))

    def test_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "discovery.json"
            _write_cache(path, "192.168.1.10:24800", "macbook")
            self.assertEqual(_read_cache(path), "192.168.1.10:24800")
            self.assertEqual(json.loads(path.read_text())["name"], "macbook")


if __name__ == "__main__":
    unittest.main()
