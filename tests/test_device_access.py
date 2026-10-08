import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions.system_command import system_command
from core import device_access


class DeviceAccessTests(unittest.TestCase):
    def test_config_requires_explicit_true(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "access.json"
            with patch.object(device_access, "CONFIG_PATH", config):
                self.assertFalse(device_access.full_access_enabled())
                for content in ('invalid', '[]', '{"full_device_access": "true"}'):
                    config.write_text(content)
                    self.assertFalse(device_access.full_access_enabled())
                config.write_text('{"full_device_access": true}')
                self.assertTrue(device_access.full_access_enabled())
                self.assertTrue(device_access.path_allowed(Path(folder)))

    def test_disabled_command_never_starts(self):
        with patch("actions.system_command.full_access_enabled", return_value=False), patch("actions.system_command.subprocess.Popen") as popen:
            self.assertFalse(json.loads(system_command({"command": "echo test"}))["ok"])
            popen.assert_not_called()

    def test_command_output_and_failure(self):
        with patch("actions.system_command.full_access_enabled", return_value=True):
            command = "Write-Output 'school assistant'" if os.name == "nt" else "printf 'school assistant'"
            result = json.loads(system_command({"command": command}))
            self.assertTrue(result["ok"], result)
            self.assertIn("school assistant", result["stdout"])
            result = json.loads(system_command({"command": "exit 7"}))
            self.assertFalse(result["ok"])
            self.assertEqual(result["exit_code"], 7)

    def test_timeout(self):
        with patch("actions.system_command.full_access_enabled", return_value=True):
            command = "Start-Sleep -Seconds 10" if os.name == "nt" else "sleep 10"
            result = json.loads(system_command({"command": command, "timeout": 1}))
            self.assertTrue(result["timed_out"], result)
            self.assertFalse(result["ok"])

    def test_invalid_input_never_starts(self):
        with patch("actions.system_command.full_access_enabled", return_value=True), patch("actions.system_command.subprocess.Popen") as popen:
            for params in ({"command": ""}, {"command": "echo hi", "timeout": 0}):
                self.assertFalse(json.loads(system_command(params))["ok"])
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
