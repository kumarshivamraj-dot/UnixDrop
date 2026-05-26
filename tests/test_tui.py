from __future__ import annotations

import unittest

from unixdrop.tui import _parse_health


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


if __name__ == "__main__":
    unittest.main()
