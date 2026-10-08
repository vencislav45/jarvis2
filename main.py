import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Time:
    """Small, useful time utility for app-level scheduling and formatting."""

    @staticmethod
    def now(tz: timezone | None = None) -> datetime:
        """Return the current UTC/local time."""
        return datetime.now(timezone.utc if tz is None and False else tz) if tz is not None else datetime.now()

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def format(value: datetime | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        dt = value or datetime.now()
        return dt.strftime(fmt)

    @staticmethod
    def fromiso(value: str) -> datetime:
        return datetime.fromisoformat(value)

    @staticmethod
    def add_seconds(value: datetime, seconds: float) -> datetime:
        return value + timedelta(seconds=seconds)

    @staticmethod
    def elapsed(start: datetime, end: datetime | None = None) -> float:
        end = end or datetime.now()
        return max((end - start).total_seconds(), 0.0)

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

import sounddevice as sd
from core.audio_input import microphone_stream, load_audio_config, update_audio_status
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    append_conversation_turn, load_recent_conversation,
)

from actions.screen_processor  import _capture_camera, _capture_screen
from core                      import llm
from core.builtin_tools        import build_registry
from core.desktop_agent        import DesktopAgent
from core.settings             import gemini_api_key, live_model, live_voice, get as get_setting
from actions.system_monitor    import SystemMonitor
from actions.proactive         import ProactiveEngine
from actions.background_monitor import check_all as monitor_check_all, list_monitors
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import get_brief_enabled


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = live_model()
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024
# Keep the mic open while the assistant speaks so "Jarvis, stop" interrupts it.
# Only enable with a headset: open speakers would let the assistant hear itself.
BARGE_IN            = get_setting("JARVIS_BARGE_IN", "0") in ("1", "true", "yes")

_get_api_key = gemini_api_key   # .env (GEMINI_API_KEY) with legacy api_keys.json fallback


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _pcm_level(data) -> float:
    """Loudness 0..1 of 16-bit mono PCM (sampled, cheap enough for every audio chunk)."""
    if len(data) < 4:
        return 0.0
    samples = memoryview(bytes(data[: len(data) - len(data) % 2])).cast("h")
    step = max(1, len(samples) // 256)
    picked = samples[::step]
    rms = (sum(s * s for s in picked) / len(picked)) ** 0.5
    return min(1.0, rms / 5000)


def _clean_transcript(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

from core.tool_declarations import TOOL_DECLARATIONS


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._asst_name     = "JARVIS"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary
        self._desktop_stop = threading.Event()
        self._desktop_running = False
        self.desktop_progress: list[str] = []
        self.tools = build_registry(self)   # every callable tool, gated and logged
        self._transcription_hints = True            # language_codes for input transcription
        self._transcription_hints_verified = False  # becomes True after a successful connect

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if self._desktop_running:
            self._desktop_stop.set()
            self.ui.write_log("SYS: Stopping the current desktop task before your new request.")
        self._session_log.append(f"User: {text}")
        append_conversation_turn("User", text)
        self.tools.note_user_input(text)
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self._submit_text(text),
            self._loop
        )

    async def _submit_text(self, text):
        while self._desktop_running:
            await asyncio.sleep(0.1)
        if self.session:
            await self.session.send_client_content(
                turns={"parts": [{"text": text}]}, turn_complete=True)

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def _drain_playback(self) -> None:
        q = self.audio_in_queue
        while q:
            try:
                q.get_nowait()
            except Exception:
                break
        self.set_speaking(False)

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        self._desktop_stop.set()
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()
        lang_entry = memory.get("identity", {}).get("language", {})
        language = lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)
        is_bulgarian = language.strip().casefold() in ("bulgarian", "български")

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        if is_bulgarian:
            _addr = 'ADDRESS: While speaking Bulgarian, address the user as "сър".'
        elif _user_name:
            _addr = f"ADDRESS: Always call the user '{_user_name}'."
        else:
            _addr = ('ADDRESS: When speaking Turkish say "efendim". '
                     'When speaking English say "sir". Never mix languages.')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        parts = [time_ctx, identity_ctx]
        if is_bulgarian:
            parts.append(
                "[BULGARIAN VOICE STYLE]\n"
                "Speak in natural, idiomatic standard Bulgarian in Cyrillic. "
                "Use correct grammar and everyday Bulgarian phrasing; avoid literal translations and English idioms. "
                "Keep spoken responses clear and concise unless detail is requested."
            )
        if mem_str:
            parts.append(mem_str)
        history = load_recent_conversation(8)
        if history:
            dialogue = "\n".join(
                f"{item.get('speaker', 'Unknown')}: {str(item.get('text', ''))[:450]}"
                for item in history
            )
            parts.append("[RECENT CONVERSATION FROM PREVIOUS RUNS — use only when relevant]\n" + dialogue)
        parts.append(sys_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription=self._transcription_config(is_bulgarian),
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": self.tools.declarations()}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=live_voice()
                    )
                )
            ),
        )

    def _transcription_config(self, is_bulgarian: bool) -> dict:
        """Tell speech recognition which languages to expect, so short Bulgarian replies such as
        "да" are not transcribed as Italian. Confirmations are checked against this transcript."""
        if not self._transcription_hints:
            return {}
        codes = [c.strip() for c in get_setting("JARVIS_SPEECH_LANGUAGES", "").split(",") if c.strip()]
        if not codes and is_bulgarian:
            codes = ["bg-BG", "en-US"]
        return {"language_codes": codes} if codes else {}

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        """Dispatch one model tool call through the registry (validation, confirmation, logging)."""
        name = fc.name
        args = dict(fc.args or {})
        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        payload = await self.tools.execute(name, args)
        if payload.get("status") == "confirmation_required":
            self.ui.write_log(f"SYS: Awaiting your confirmation — {payload['action']}")
        elif payload.get("ok") is False:
            self.ui.write_log(f"ERR: {name} — {str(payload.get('error') or payload.get('result'))[:120]}")

        if self._desktop_running:
            self.ui.set_state("THINKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")
        print(f"[JARVIS] 📤 {name} → {str(payload)[:100]}")
        return types.FunctionResponse(id=fc.id, name=name, response=payload)

    # ── Tools that need the live session (registered in core/builtin_tools.py) ──

    @property
    def desktop_running(self) -> bool:
        return self._desktop_running

    async def tool_desktop_task(self, args: dict) -> dict:
        if self._desktop_running:
            return {"ok": False, "error": "A desktop task is already running. Wait for its result."}
        self._desktop_running = True
        self._desktop_stop.clear()
        self.desktop_progress = []

        def progress(message: str) -> None:
            self.desktop_progress.append(message)
            self.ui.write_log(f"[Task] {message}")

        try:
            async with genai.Client(api_key=gemini_api_key()).aio as reasoning_client:
                from types import SimpleNamespace
                agent = DesktopAgent(SimpleNamespace(aio=reasoning_client), TOOL_DECLARATIONS,
                                     self._execute_tool, _capture_screen, self._desktop_stop, progress)
                outcome = await agent.run(args.get("goal", ""), "\n".join(self._session_log[-12:]))
        finally:
            self._desktop_running = False
        self.ui.show_content("DESKTOP TASK — " + outcome["status"].upper(),
                             outcome["summary"] + "\n\n" + "\n".join(
                                 f"{step['step']}. {step['tool']} {step['action']}" for step in outcome["steps"]))
        result = {"status": outcome["status"], "summary": outcome["summary"],
                  "steps_executed": len(outcome["steps"]),
                  "completed_steps": outcome["steps"][-8:] if outcome["status"] != "complete" else []}
        pending = self.tools.gate.pending()
        if pending:
            # A step needed the user's approval; surface it so the user can confirm it now.
            result["awaiting_confirmation"] = [p.to_prompt() for p in pending]
        return result

    async def tool_screen_process(self, args: dict) -> str:
        now = time.monotonic()
        cooldown = 4.0  # seconds — covers echo window after speaking ends
        if self._vision_busy or (now - self._vision_last_time) < cooldown:
            print(f"[Vision] ⏳ Cooldown active ({max(0, cooldown - (now - self._vision_last_time)):.1f}s remaining)")
            return "Vision is still processing the previous request. I will not call this again."
        self._vision_busy = True
        self._vision_last_time = now
        angle = args.get("angle", "screen").lower()
        user_text = args.get("text", "What do you see?")
        try:
            if angle == "camera":
                img_b, mime_t = await asyncio.to_thread(_capture_camera)
                self.ui.start_camera_stream()
                self._vision_cam_active = True
                stall = "camera"
            else:
                img_b, mime_t = await asyncio.to_thread(_capture_screen)
                stall = "screen"
        except Exception:
            self._vision_busy = False
            raise
        print(f"[Vision] {stall}: {len(img_b):,} bytes")
        self._pending_vision = (img_b, mime_t, user_text, angle)
        return (f"[VISION_ACTIVE] {stall.capitalize()} captured. "
                f"Immediately say ONE short natural sentence in the user's own language, "
                f"telling them you are looking at their {stall} right now. "
                f"Do NOT describe or guess content — the actual image arrives in the NEXT message.")

    async def tool_close_camera(self, args: dict) -> str:
        self.ui.stop_camera_stream()
        return "Camera closed."

    async def tool_shutdown(self, args: dict) -> str:
        self.ui.write_log("SYS: Shutdown requested.")

        async def _do_shutdown():
            await self._save_session_summary()
            if self.session:
                try:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": "Say a brief natural goodbye to the user."}]},
                        turn_complete=True,
                    )
                except Exception:
                    pass
            await asyncio.sleep(1.5)
            import os as _os
            _os._exit(0)

        asyncio.create_task(_do_shutdown())
        return "Shutting down."

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def enqueue_audio(data, sample_rate):
            if self.out_queue.full():
                self.out_queue.get_nowait()
            self.out_queue.put_nowait({"data": data, "mime_type": f"audio/pcm;rate={sample_rate}"})

        def callback(data, sample_rate):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted:
                self.ui.set_audio_level(_pcm_level(data))   # HUD reacts to your voice while listening
            if (BARGE_IN or not jarvis_speaking) and not self.ui.muted and not self._phone_active:
                loop.call_soon_threadsafe(enqueue_audio, data, sample_rate)

        while True:
            audio_config = load_audio_config()
            selected = audio_config.get("microphone")
            try:
                with microphone_stream(callback, selected) as (device_name, sample_rate):
                    self.ui.write_log(f"Mic: {device_name} ({sample_rate} Hz)")
                    while load_audio_config() == audio_config:
                        await asyncio.sleep(1)
                        state = "Muted" if self.ui.muted else "Paused while assistant speaks" if self._is_speaking else "Phone microphone active" if self._phone_active else "Listening"
                        update_audio_status(message=f"{device_name} ({sample_rate} Hz) — {state}")
            except Exception as e:
                update_audio_status(message=str(e), level=0)
                self.ui.write_log(f"Microphone unavailable: {e}. Text chat is still active. Use Settings > Microphone.")
                # Retry only after a selection change; never reconnect the AI session.
                while load_audio_config() == audio_config:
                    await asyncio.sleep(1)

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()
                                # Confirmations are validated against what the user actually said.
                                self.tools.note_user_input(txt)
                                self.tools.last_trigger = " ".join(in_buf)

                        if sc.interrupted and BARGE_IN:
                            # The user talked over the assistant: stop playback, keep listening.
                            self._drain_playback()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                append_conversation_turn("User", full_in)
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                append_conversation_turn(self._asst_name, full_out)
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        stream.start()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                self.ui.set_audio_level(_pcm_level(batch))   # HUD core/spectrum follow the voice
                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today", 6)

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        lang_clause = f" Respond in {lang}." if lang else ""
        if lang.strip().casefold() in ("bulgarian", "български"):
            name_clause = ' Address the user as "сър" and speak natural Bulgarian.'
        else:
            name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = f" Respond in {lang}." if lang else ""

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    # World + Bulgaria feeds are fetched together; allow a little extra time.
                    news_text = await asyncio.wait_for(news_done, timeout=6.0)
                except Exception:
                    news_text = ""

                if not self.session:
                    return

                if news_text and len(news_text) > 60 and not news_text.startswith("No news found"):
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)
                    headline_count = len(re.findall(r"(?m)^\s*\d+\.\s", news_text))
                    all_headlines_instruction = (
                        f"Read all {headline_count} numbered headlines in order, not just the first three. "
                        f"Say there are {headline_count} headlines, name each publisher, and do not omit any. "
                        if headline_count else
                        "Read every headline listed, in order, naming each publisher; do not omit any. "
                    )

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        f"{all_headlines_instruction}Read the WORLD section first, then introduce the "
                        f"BULGARIA section (e.g. \"And from Bulgaria:\") before its headlines. Then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from google import genai as _genai
            client = _genai.Client(api_key=_get_api_key())
            resp   = await asyncio.to_thread(
                llm.generate,
                client,
                model="gemini-3.1-flash-lite",
                contents=prompt,
            )
            summary = (resp.text or "").strip()
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if self._desktop_running or speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not self._desktop_running and not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or self._desktop_running:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    # Same bookkeeping as the PC text box: history, and the reply that confirmations check.
                    self.tools.note_user_input(text)
                    self._session_log.append(f"User: {text}")
                    append_conversation_turn("User", text)
                    self._last_user_speech = time.monotonic()
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print(f"[JARVIS] Connecting with {LIVE_MODEL}...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False
                    self._transcription_hints_verified = True

                    print("[JARVIS] Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled)
                    if not self._briefing_sent and get_brief_enabled():
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Language hints for speech recognition are optional: if the very first
                # connection with them fails, drop them rather than stay offline.
                rejected = any(k in err_str.lower() for k in ("1007", "invalid", "language", "transcription", "unknown name"))
                if self._transcription_hints and not self._transcription_hints_verified and rejected:
                    self._transcription_hints = False
                    self.ui.write_log("SYS: Speech language hints were not accepted; continuing without them.")
                    self._conn_backoff = 1
                    continue

                # Prompt again only for an explicitly reported invalid key.
                if "API key not valid" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                if "1007" in err_str:
                    self._conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self.ui.write_log(
                        f"ERR: Gemini Live rejected an audio/session request (1007). "
                        f"Retrying in {self._conn_backoff}s; this is not an API-key error."
                    )
                    self.ui.set_state("SLEEPING")
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Bağlantı kurulamadı — {_conn_backoff}s sonra tekrar deneniyor. "
                        "(VPN gerekiyor olabilir)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()
