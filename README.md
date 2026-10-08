# ⚙️ MARK L (50)
### The Ultimate Cross-Platform Personal AI Assistant — By FatihMakes

> 📺 **[Watch the full setup video on YouTube](https://www.youtube.com/@FatihMakes)**

A real-time voice AI that can hear, see, understand, and control your computer — on any OS. Supports Windows, macOS, and Linux. Built on the Gemini Live API for native audio streaming, delivering zero subscriptions and total digital autonomy.

---

## ✨ Overview

MARK L is where the assistant stops being a tool and starts being a presence. It remembers yesterday's conversation, watches the topics you care about, and speaks first when it has something worth saying. The goal of this build was continuity — JARVIS should feel like it never fully left, even after you close it.

It's not just an assistant — it's an extension of your digital life.

---

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via Gemini Live API |
| 🖥️ System Control | Launch apps, adjust volume/brightness, WiFi, shortcuts, power — all by voice |
| 🧩 Autonomous Tasks | High-level planning for complex multi-step goals via agent mode |
| 👁️ Visual Awareness | Real-time screen capture and webcam vision piped into your main Gemini session |
| 🧠 Persistent Memory | Deeply remembers projects, preferences, and personal context across sessions |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |
| 🌅 Morning Briefing | On first boot: greets you, reads the time, recaps yesterday, and fetches live news |
| 🔔 Proactive 2.0 | Time-aware, context-aware check-ins — knows the time of day, your projects, and what you've been discussing |
| 🗓️ Session Memory | Summarises each conversation and mentions it naturally next morning — consumed after use, never repeats |
| 👁️‍🗨️ Background Monitoring | User-configured topic watching — checks for new headlines once a day and alerts naturally |
| 📊 Hardware Monitoring | Continuous CPU, RAM, GPU and temperature telemetry with localized voice alerts |
| 🌤️ Weather Report | Live weather data for your city, personalized from memory |
| 🗺️ Dynamic Content Panel | Scrollable display layer beneath the HUD that renders web results, news, and search data |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini Grounded first, DDG fallback |
| ⏰ Smart Reminders | OS-native scheduled notifications (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) |
| ✈️ Flight Finder | Live flight price and availability lookup |
| 🎮 Game Updater | Checks and triggers game updates on Steam and Epic Games on demand |
| 📂 File Processor | Read, summarize, and answer questions about local files |
| 💻 Code Helper | Inline code review, debugging, and generation |
| 🌐 Browser Control | Open URLs, navigate tabs, and interact with the browser by voice |
| 📨 Send Message | Compose and send messages through WhatsApp, Telegram, and more |
| 🎬 YouTube Control | Search, play, and control YouTube playback by voice |
| 🖱️ Desktop Control | Taskbar, window management, and desktop-level operations |
| 🧑‍💻 Silent Language Memory | Detects spoken language on first use — all future sessions adapt automatically |
| 📱 Remote Dashboard | Control the assistant from your phone via QR code pairing |
| ⚡ Auto-Start on Boot | Registers with the OS startup system (registry / LaunchAgent / .desktop) |
| 📋 Clipboard Intelligence | Copy any text → floating panel with Translate / Summarise / Explain / Fix |
| 🎨 Assistant Customization | Change the assistant name and your name from the UI — takes effect immediately |

---

## 🆕 What's New in Mark L

### 🗓️ Session Memory — JARVIS Remembers Yesterday
At the end of every session, JARVIS generates a 1-2 sentence summary of what was discussed and saves it to memory. The next morning, it's mentioned naturally in the briefing:
> *"Good morning, sir — it's 09:15. Yesterday you were working on the Mark L background monitoring feature. Fetching today's headlines now."*

The summary is consumed immediately after use — it never repeats in future briefings and adds zero long-term bloat to memory.

### 👁️‍🗨️ Background Monitoring — JARVIS Watches While You're Away
Tell JARVIS to monitor any topic and it checks for new developments once a day using DuckDuckGo news. When a headline changes, it reports back naturally in your language:
> *"Efendim, takip ettiğiniz yapay zeka haberlerinde bir gelişme var: Google yeni bir model duyurdu."*

Fully opt-in — JARVIS monitors nothing without being explicitly asked. Crypto, financial, and trading topics are blocked at the code level regardless of what is requested. Same headline never triggers twice.

### 🔔 Proactive System 2.0 — Context-Aware, Time-Aware, Non-Repetitive
The proactive engine was rebuilt from the ground up. Instead of a generic check-in after 15 minutes of silence, JARVIS now:
- Knows the **time of day** — morning tone differs from evening tone
- Knows your **active projects** from memory and can ask how something is going
- Knows your **monitored topics** and can bring one up naturally
- Knows **what you were just talking about** (last 8 conversation turns)
- **Rotates** between three focus areas so it never opens with the same line twice
- Has a 20-minute cooldown (up from 10) — less intrusive, more meaningful

### 👁️ Instant Vision Acknowledgment — No More Silent Waiting
When you ask JARVIS to look at your screen or camera, it no longer goes silent while processing. It immediately says something natural ("Looking at your screen now, sir" / "Ekrana bakıyorum efendim") while the capture runs. The actual analysis follows as the next response.

### 📰 Parallel News Search — First Result Wins
News queries now run Gemini Grounded Search and DuckDuckGo news simultaneously in two threads. Whichever delivers a valid result first is used; the other is silently discarded. A Gemini 503 error no longer delays results — the DDG fallback is already running in parallel.

---

## 🗺️ Mark Roadmap

| Mark | Focus |
|---|---|
| **XLVIII** | Instant interrupt · parallel news · two-phase briefing · exponential backoff · vision cooldown |
| **XLIX** | Auto-start · clipboard intelligence · assistant customization |
| **L** | Session memory · background monitoring · proactive 2.0 · instant vision · parallel news search |
| **LI+** | Plugin system · email · quiz mode · calorie counter · calendar |

---

## ⚡ Quick Start

```bash
git clone https://github.com/FatihMakes/Mark-L.git
cd Mark-L
pip install -r requirements.txt
python main.py
```

> ⚠️ **Installation Note:** Some OS-specific dependencies are not bundled in `requirements.txt` to keep the repo lightweight. If you hit a `ModuleNotFoundError`, install the missing package with `pip install <module_name>`.

---

## Desktop task reasoning

Multi-step requests now use `desktop_task`, a dedicated Gemini reasoning loop
separate from the live voice model. It can inspect the screen, operate existing
desktop/file/browser tools, correct errors, and review observed results before
reporting completion. Simple commands continue to use direct tools.

Restart `main.py` after installing these changes. Example requests:

- "Create a School Project folder in Documents, write a short project description
  in README.txt, then read it back and check it was saved."
- "Find the PDF files in Downloads and list the three largest with their sizes."
- "Open Notepad, type my project introduction, save it to Documents, and verify
  the file contains the introduction."

Progress appears in the log as `[Task]` entries. A result panel reports complete,
blocked, or stopped and lists executed steps. The Interrupt button stops further
task actions; an action already in progress may finish. A typed follow-up stops
the task before submitting the new request. Use the Interrupt button during a
long desktop task; voice commands are not a guaranteed immediate cancellation.

`config/intelligence.json` selects the reasoning model and bounds the task
(24 reasoning rounds, 48 actions by default). This uses the existing Gemini key
and normal API quota/billing. Rate-limit responses pause and retry the reasoning
request at most twice, preserving completed work. Live voice uses Gemini 2.5 Flash
Native Audio, and desktop reasoning uses Gemini 3.8 Flash. A separate model review improves
completion checking but is not a guarantee against AI mistakes.

Completed user and assistant turns are stored locally in
`memory/conversation_history.json` (up to 2,000 turns); the 8 latest entries are
included as context on startup. Ask the assistant about a past topic to search
the saved transcript and long-term facts. Remove that file to clear saved dialogue. The
assistant does not rewrite its own source code or grant itself additional OS
privileges.

The Razer Seiren Mini is selected in `config/audio.json`. Settings → Microphone /
Input Level lets you change the selection, retry opening it, and watch the live
input level. Changes apply without restarting the connected assistant.

Developer checks: `python -m unittest discover -s tests`. The optional
`python tests/live_desktop_smoke.py` uses the real Gemini API and creates two
files inside a unique `tests/live-smoke-*` directory, then checks their contents
independently. It does not use screen capture or operate other folders.

## 📋 Requirements

### Device access for this school project

`config/device_access.json` enables `full_device_access`. Restart the assistant
after updating the code. The file manager can now work across accessible drives,
screenshots can be saved outside the home folder, and the `system_command` tool
can run PowerShell commands with the assistant's Windows account permissions.
Existing mouse, keyboard, clipboard, app, and screen controls remain available.

Try: "List the files in E:\\Mark-L-main", "Show my running processes", or
"Use PowerShell to show the free space on my drives".

Commands return output, errors, and exit codes, with a 30-second default timeout
(maximum 120 seconds). The UI log displays commands. Windows access controls and
UAC still apply; this setting does not grant administrator privileges. Tool results
and captured screen content may be sent to the configured AI provider.

Set `full_device_access` to `false` to disable the new command tool and restore
home-folder limits for the file manager and screenshot saves. This is not a global
sandbox: the project's existing app, code execution, and desktop tools still run
with your account permissions. Move the pointer to a screen corner to trigger
PyAutoGUI's existing mouse/keyboard fail-safe; it does not cancel shell commands.

Run checks with `python -m unittest discover -s tests`.

| Requirement | Details |
| --- | --- |
| **OS** | Windows 10/11, macOS, or Linux |
| **Python** | 3.11 or 3.12 |
| **Microphone** | Required for voice interaction |
| **API Key** | Free Gemini API key (`config/api_keys.json`) |

---

## 🧱 Assistant core (tools, safety, memory)

```
voice/text ─► Gemini Live (understands intent, picks tool + arguments)
                 │ function call
                 ▼
          core/tool_registry.py ── core/permissions.py   LOW → run
                 │                  (risk + confirmation)  MEDIUM/HIGH → ask the user first
                 ▼
          handler (actions/*, actions/messaging/*) ──► result checked ──► core/action_log.py
                 │
                 ▼
          honest result back to the model ─► short spoken reply
```

| Module | Role |
|---|---|
| `core/settings.py` | `.env` config (`GEMINI_API_KEY`, model, voice, app paths); legacy `api_keys.json` fallback |
| `core/tool_declarations.py` | What the model may call |
| `core/builtin_tools.py` | Maps each declaration to its handler (startup fails if one is missing) |
| `core/tool_registry.py` | Runs tools: confirmation gate, retry for read-only tools, never reports empty results as success |
| `core/permissions.py` | LOW / MEDIUM / HIGH classification; confirmations are released only by the **user's** reply |
| `core/action_log.py` | `logs/actions.jsonl` — time, command, tool, args, result, error (secrets and message bodies redacted) |
| `core/diagnostics.py` | "Check yourself" — `python -m core.diagnostics` |
| `actions/messaging/` | WhatsApp Web, Instagram, Messenger (browser, verified) and Viber/Telegram/Signal/Discord (draft) |
| `memory/memory_manager.py` | Categories: preferences, projects, people, tasks, identity, notes, temporary; forget last / by topic |

**Confirmation flow:** "Message Mom on WhatsApp that I'm coming home at 7" → `find_contact` → `send_message`
is held → "Send to Mom on WhatsApp: 'I'm coming home at 7'?" → you say "yes" → `confirm_action` runs the exact
held action. A reply that isn't a clear yes ("no", "wait", "yes but…") keeps it blocked; pending actions expire
after 2 minutes. To let a tool skip confirmation for MEDIUM risk, list it in `config/permissions.json`.

**Adding a tool:** declare it in `core/tool_declarations.py`, register its handler in `core/builtin_tools.py`,
and add a rule to `permissions.classify` if it can change anything.

**Your Chrome, your accounts:** opening sites and searching always happen in your own Google Chrome. Reading
pages, clicking and sending Instagram/WhatsApp/Messenger messages go through the **Jarvis Bridge** extension
(`chrome_extension/`), because Chrome does not let automation tools control your everyday profile. Install it
once: `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → choose the `chrome_extension`
folder. It connects only to Jarvis on `127.0.0.1:48761`, and Chrome shows a "started debugging this browser"
banner while Jarvis acts in a tab. `JARVIS_BROWSER_MODE=separate` switches back to a standalone window
with its own profile.

**Messaging limits:** none of these services offers a personal-account messaging API. The assistant uses
your signed-in tabs and never logs in, solves CAPTCHAs or bypasses 2FA. Viber and the other desktop apps can't show which chat is open, so messages
are typed as a draft for you to send (`JARVIS_DESKTOP_MESSAGING=send` presses Enter but stays unverified).
Web UIs change, so if a selector breaks the integration reports it instead of guessing.

## 🗂️ Project Structure

```
Mark L/
├── main.py                   # Core loop — Gemini Live session, audio I/O, tool dispatch
├── ui.py                     # PyQt6 HUD — waveform, log panel, interrupt button, camera feed
├── setup.py                  # First-run configuration wizard
├── actions/
│   ├── web_search.py         # Gemini + DDG parallel search (news, research, price, compare)
│   ├── screen_processor.py   # Screen capture & webcam vision via Gemini Live
│   ├── background_monitor.py # User-configured topic watching — daily DDG check, no crypto
│   ├── proactive.py          # Proactive 2.0 — time/context/rotation-aware check-ins
│   ├── reminder.py           # OS-native scheduled notifications
│   ├── system_monitor.py     # CPU / RAM / GPU / temperature telemetry
│   ├── computer_settings.py  # Volume, brightness, WiFi, power
│   ├── computer_control.py   # Keyboard shortcuts, mouse, window management
│   ├── open_app.py           # Application launcher
│   ├── browser_control.py    # Web browser control
│   ├── file_controller.py    # File system operations
│   ├── file_processor.py     # Document reading and summarization
│   ├── send_message.py       # Messaging integration
│   ├── weather_report.py     # Live weather data
│   ├── flight_finder.py      # Flight search
│   ├── youtube_video.py      # YouTube playback control
│   ├── game_updater.py       # Game update management (Steam / Epic)
│   ├── code_helper.py        # Code review and generation
│   ├── dev_agent.py          # Developer task agent
│   └── desktop.py            # Desktop and taskbar control
├── memory/
│   ├── memory_manager.py     # Load/save long_term.json — sessions, monitors, identity
│   └── long_term.json        # Persistent store: identity, preferences, projects, sessions, monitors
├── core/
│   └── prompt.txt            # Assistant personality and tool-routing rules
└── config/
    └── api_keys.json         # API key, OS setting, assistant name, user name
```

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect with the Creator

Engineered by a developer building a real-world JARVIS-style assistant.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
| --- | --- |
| YouTube | [@FatihMakes](https://www.youtube.com/@FatihMakes) |
| Instagram | [@fatihmakes](https://www.instagram.com/fatihmakes) |
