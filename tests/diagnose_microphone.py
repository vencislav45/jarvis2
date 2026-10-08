"""Run manually to compare microphone drivers; no audio is saved or uploaded."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import sounddevice as sd
from core.audio_input import _open_device, load_audio_config


if __name__ == "__main__":
    name = load_audio_config().get("microphone", "Razer Seiren Mini")
    hosts = sd.query_hostapis()
    print("Speak into your microphone during this test. No recording is saved.", flush=True)
    for device in sd.query_devices():
        if name.casefold() not in device["name"].casefold() or device["max_input_channels"] < 1:
            continue
        peaks = []
        try:
            stream, rate = _open_device(device, lambda data, rate: peaks.append(
                int(np.max(np.abs(np.frombuffer(data, dtype="<i2").astype(np.int32))))))
            try:
                print(f"Testing {device['name']} / {hosts[device['hostapi']]['name']}…", flush=True)
                time.sleep(3)
            finally:
                stream.stop()
                stream.close()
            print(f"Rate={rate}, callbacks={len(peaks)}, peak={max(peaks, default=0)} / 32768", flush=True)
        except Exception as exc:
            print(f"Unavailable: {exc}", flush=True)
