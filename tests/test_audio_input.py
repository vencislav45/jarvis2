import unittest
from unittest.mock import Mock, patch

from core.audio_input import microphone_stream


class MicrophoneTests(unittest.TestCase):
    def test_failed_default_uses_fallback_and_closes(self):
        devices = [{"index": 0, "name": "fallback", "max_input_channels": 1},
                   {"index": 1, "name": "default", "max_input_channels": 1}]
        stream = Mock()
        with patch("core.audio_input.sd.query_devices", return_value=devices), patch("core.audio_input.sd.default.device", [1, 0]), patch("core.audio_input._open_device", side_effect=[RuntimeError("unavailable"), (stream, 44100)]) as opener:
            with microphone_stream(Mock()) as selected:
                self.assertEqual(selected, ("fallback", 44100))
            self.assertEqual(opener.call_args_list[0].args[0]["name"], "default")
            stream.stop.assert_called_once()
            stream.close.assert_called_once()

    def test_no_devices_has_clear_error(self):
        with patch("core.audio_input.sd.query_devices", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "No working microphone"):
                with microphone_stream(Mock()):
                    pass

    def test_selected_microphone_never_falls_back_to_another(self):
        devices = [{"index": 0, "name": "Webcam", "max_input_channels": 1},
                   {"index": 1, "name": "Microphone (Razer Seiren Mini)", "max_input_channels": 1}]
        with patch("core.audio_input.sd.query_devices", return_value=devices), patch("core.audio_input._open_device", side_effect=RuntimeError("USB unavailable")) as opener:
            with self.assertRaisesRegex(RuntimeError, "No working microphone"):
                with microphone_stream(Mock(), "Razer Seiren Mini"):
                    pass
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(opener.call_args.args[0]["index"], 1)
