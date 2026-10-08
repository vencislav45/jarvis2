"""Open the selected microphone with a format supported by its driver."""
from contextlib import contextmanager
import json
import threading
import time

import numpy as np
import sounddevice as sd
from core.device_access import BASE_DIR

CONFIG_PATH = BASE_DIR / "config" / "audio.json"
_lock = threading.Lock()
_status = {"message": "Waiting for microphone", "level": 0, "updated": 0}


def load_audio_config():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def save_microphone(name):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config = load_audio_config()
    config["microphone"] = name
    config["revision"] = time.time_ns()
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_PATH)


def update_audio_status(**values):
    with _lock:
        _status.update(values)


def audio_status():
    with _lock:
        result = dict(_status)
    if time.monotonic() - result["updated"] > 1:
        result["level"] = 0
    return result


def input_names():
    return sorted({d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0})


def _open_device(device, callback):
    rates = list(dict.fromkeys([16000, int(device["default_samplerate"]), 48000, 44100]))
    channels = list(dict.fromkeys([1, min(2, int(device["max_input_channels"]))]))
    errors = []
    stream = None
    for rate in rates:
        for count in channels:
            if count < 1:
                continue
            candidate = None
            try:
                sd.check_input_settings(device=device["index"], samplerate=rate,
                                        channels=count, dtype="int16")
                def on_audio(data, frames, timing, status, sample_rate=rate):
                    mono = data.astype("float32").mean(axis=1).astype("<i2") if data.shape[1] > 1 else data.astype("<i2")
                    rms = float(np.sqrt(np.mean(mono.astype("float32") ** 2))) / 32768
                    # A dBFS meter makes normal speech visible without changing audio gain.
                    level = int(np.clip((20 * np.log10(max(rms, 1e-6)) + 60) / 60 * 100, 0, 100))
                    update_audio_status(level=level, updated=time.monotonic())
                    callback(mono.tobytes(), sample_rate)
                candidate = sd.InputStream(device=device["index"], samplerate=rate,
                                           channels=count, dtype="int16", blocksize=0,
                                           callback=on_audio)
                candidate.start()
                stream = candidate
                break
            except (sd.PortAudioError, ValueError) as exc:
                errors.append(str(exc))
                if candidate is not None:
                    candidate.close()
        if stream is not None:
            break
    if stream is None:
        raise RuntimeError(f"Cannot open {device['name']}: {errors[-1] if errors else 'no input channels'}")
    return stream, rate


@contextmanager
def microphone_stream(callback, preferred=None):
    devices = sd.query_devices()
    default_index = sd.default.device[0]
    candidates = [d for d in devices if d["max_input_channels"] > 0]
    if preferred:
        exact = [d for d in candidates if d["name"] == preferred]
        candidates = exact or [d for d in candidates if preferred.casefold() in d["name"].casefold()]
        if not candidates:
            raise RuntimeError(f"Selected microphone not found: {preferred}. Check its USB connection or choose another microphone.")
    candidates.sort(key=lambda d: d["index"] != default_index)
    errors = []
    stream = None
    for device in candidates:
        try:
            stream, rate = _open_device(device, callback)
            break
        except (RuntimeError, sd.PortAudioError, ValueError) as exc:
            errors.append(str(exc))
    if stream is None:
        raise RuntimeError("No working microphone. " + (errors[-1] if errors else "No input devices found."))
    try:
        update_audio_status(message=f"{device['name']} ({rate} Hz)")
        yield device["name"], rate
    finally:
        stream.stop()
        stream.close()
