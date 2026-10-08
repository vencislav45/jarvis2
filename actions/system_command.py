"""Run user-requested system commands with the assistant process's permissions."""
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

from core.device_access import BASE_DIR, full_access_enabled

OUTPUT_LIMIT = 16000


def system_command(parameters: dict, player=None) -> str:
    if not full_access_enabled():
        return json.dumps({"ok": False, "error": "System commands are disabled in config/device_access.json."})
    try:
        command = parameters.get("command", "")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("A nonempty command is required.")
        timeout = int(parameters.get("timeout", 30))
        if not 1 <= timeout <= 120:
            raise ValueError("timeout must be between 1 and 120 seconds.")
        cwd = Path(parameters.get("cwd") or BASE_DIR).expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError("cwd must be a directory.")
        if os.name == "nt":
            script = "$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8;\n" + command
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW}
        else:
            argv = ["/bin/sh", "-c", command]
            options = {"start_new_session": True}
        if player:
            player.write_log(f"[System command] {command}")
        # Spool output to disk so verbose commands cannot fill process memory.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, **options)
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=10, **options)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            output = {}
            for name, stream in (("stdout", stdout), ("stderr", stderr)):
                stream.seek(0)
                raw = stream.read(OUTPUT_LIMIT + 1)
                output[name] = raw[:OUTPUT_LIMIT].decode("utf-8", errors="replace")
                output[name + "_truncated"] = len(raw) > OUTPUT_LIMIT
        return json.dumps({"ok": process.returncode == 0 and not timed_out,
                           "exit_code": process.returncode, "timed_out": timed_out,
                           "cwd": str(cwd), **output}, ensure_ascii=False)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})
