from __future__ import annotations

import unittest
from pathlib import Path

from unixdrop.deskflow_setup import _client_script_body


class DeskflowSetupTests(unittest.TestCase):
    def test_client_launcher_probes_tcp_without_nc_and_fails_fast(self) -> None:
        body = _client_script_body(
            server_ip="192.0.2.10:24800",
            config_dir=Path("/tmp/deskflow"),
            settings_file=Path("/tmp/deskflow/client.conf"),
            client_name="thinkpad",
            command=("/usr/bin/deskflow", "legacy"),
        )

        self.assertIn("socket.create_connection", body)
        self.assertIn("No Deskflow server is accepting TCP connections", body)
        self.assertNotIn("first_endpoint", body)


if __name__ == "__main__":
    unittest.main()
