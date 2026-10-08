"""Manual API integration test: creates files only inside its own test directory.

Uses the configured Gemini API key and incurs normal API usage.
"""
import asyncio
import json
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from google import genai
from google.genai import types
from core.desktop_agent import DesktopAgent
from core.device_access import BASE_DIR
from core.settings import gemini_api_key
from core.tool_declarations import TOOL_DECLARATIONS
from actions.file_controller import file_controller


async def main():
    declarations = [d for d in TOOL_DECLARATIONS if d["name"] == "file_controller"]
    folder = (BASE_DIR / "tests" / ("live-smoke-" + uuid.uuid4().hex[:8])).resolve()
    folder.mkdir()
    async def execute(fc):
        args = dict(fc.args)
        if args.get("action") not in {"list", "create_file", "create_folder", "copy", "read", "info"}:
            raise ValueError("This integration test permits only creation and inspection.")
        for field in ("path", "destination"):
            if args.get(field) and not Path(args[field]).resolve().is_relative_to(folder):
                raise ValueError("Test paths must stay inside the test folder.")
        if args.get("name") and Path(args["name"]).name != args["name"]:
            raise ValueError("Test file name must be a plain name.")
        return types.FunctionResponse(name=fc.name, response={"result": file_controller(args)})
    def no_screen():
        raise ValueError("This file-only test does not need screen access.")
    key = gemini_api_key()
    async with genai.Client(api_key=key).aio as client:
        from types import SimpleNamespace
        agent = DesktopAgent(SimpleNamespace(aio=client), declarations, execute, no_screen,
                             threading.Event(), lambda msg: print(msg, flush=True))
        result = await agent.run(f"Inside {folder}, create notes.txt containing exactly 'School assistant test'. Copy it to backup.txt. Read both files to verify their contents match. Use only the provided file tool and exact absolute paths.")
    print(json.dumps({"status": result["status"], "summary": result["summary"], "steps": len(result["steps"]), "folder": str(folder)}, indent=2))
    assert result["status"] == "complete", result["summary"]
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "School assistant test"
    assert (folder / "backup.txt").read_text(encoding="utf-8") == "School assistant test"
    print("PASS: Actual file contents verified independently.")


if __name__ == "__main__":
    asyncio.run(main())
