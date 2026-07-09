from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from unixdrop.service_install import linux_service_text, mac_agent_payload, write_linux_service, write_mac_agent


class ServiceInstallTests(unittest.TestCase):
    def test_linux_service_uses_python_module_without_project_placeholder(self) -> None:
        text = linux_service_text("/opt/unixdrop/bin/python")

        self.assertIn("ExecStart=/opt/unixdrop/bin/python -m unixdrop.node", text)
        self.assertNotIn("__PROJECT_DIR__", text)
        self.assertNotIn("WorkingDirectory", text)

    def test_mac_agent_uses_python_module_without_working_directory(self) -> None:
        payload = mac_agent_payload("/opt/unixdrop/bin/python", Path("/Users/example"))

        self.assertEqual(payload["ProgramArguments"], ["/opt/unixdrop/bin/python", "-m", "unixdrop.node"])
        self.assertNotIn("WorkingDirectory", payload)
        self.assertTrue(payload["StandardOutPath"].endswith("Library/Logs/unixdrop.log"))

    def test_service_writers_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            linux_target = write_linux_service(target_dir=root / "systemd", python_executable="/bin/python3")
            mac_target = write_mac_agent(
                target_dir=root / "launchd",
                python_executable="/bin/python3",
                home_dir=root,
            )

            self.assertTrue(linux_target.exists())
            self.assertIn("/bin/python3 -m unixdrop.node", linux_target.read_text(encoding="utf-8"))
            self.assertTrue(mac_target.exists())
            with mac_target.open("rb") as handle:
                payload = plistlib.load(handle)
            self.assertEqual(payload["ProgramArguments"], ["/bin/python3", "-m", "unixdrop.node"])


if __name__ == "__main__":
    unittest.main()
