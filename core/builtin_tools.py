"""Registers every tool the assistant can call.

Plain action modules are wrapped here. Tools that need the live voice session
(vision, desktop_task, shutdown) are methods on JarvisLive and passed in as ``live``.
"""
from __future__ import annotations

import asyncio

from core import llm
from core.action_log import recent_actions
from core.tool_declarations import TOOL_DECLARATIONS
from core.tool_registry import ToolRegistry

# Read-only tools that may be retried once after a transient network error.
RETRY_SAFE = {"web_search", "news_report", "weather_report", "system_status", "search_memory",
              "memory_list", "find_contact", "self_check"}


def build_registry(live) -> ToolRegistry:
    from actions.background_monitor import add_monitor, list_monitors, remove_monitor
    from actions.browser_control import browser_control
    from actions.code_helper import code_helper
    from actions.computer_control import computer_control
    from actions.computer_settings import computer_settings
    from actions.desktop import desktop_control
    from actions.dev_agent import dev_agent
    from actions.file_controller import file_controller
    from actions.file_processor import file_processor
    from actions.flight_finder import flight_finder
    from actions.game_updater import game_updater
    from actions.news_report import news_report
    from actions.open_app import open_app
    from actions.reminder import reminder
    from actions.send_message import find_contact, send_message
    from actions.system_command import system_command
    from actions.system_monitor import get_system_status
    from actions.weather_report import weather_action
    from actions.web_search import web_search
    from actions.youtube_video import youtube_video
    from core import diagnostics
    from memory import memory_manager as mm

    ui, speak = live.ui, live.speak
    registry = ToolRegistry()

    from actions import messaging
    from actions.chrome_bridge import browser_mode, get_bridge
    # e.g. "Instagram is signed out in this browser — please sign in in the tab I opened"
    messaging.set_notifier(lambda message: ui.write_log(f"SYS: {message}"))
    if browser_mode() == "my_chrome":
        get_bridge()   # start listening now so the Chrome extension connects before it is needed

    def show(title: str, text: str) -> None:
        try:
            ui.show_content(title, text)
        except Exception:  # noqa: BLE001 - the panel is cosmetic
            pass

    def run_web_search(args: dict):
        result = web_search(parameters=args, player=ui)
        if result and not str(result).startswith(("No results", "Search failed")):
            query = args.get("query") or ", ".join(args.get("items", []))
            show(f"{args.get('mode', 'search').upper()} — {query[:38]}", result)
        return result

    def run_news(args: dict):
        result = news_report(parameters=args, player=ui)
        show("NEWS", result)
        return result

    def run_file_processor(args: dict):
        if not args.get("file_path") and getattr(ui, "current_file", None):
            args = {**args, "file_path": ui.current_file}
        return file_processor(parameters=args, player=ui, speak=speak)

    def run_monitor(args: dict):
        action, topic = args.get("action", "").lower().strip(), args.get("topic", "").strip()
        if action == "add" and topic:
            return add_monitor(topic)
        if action == "remove" and topic:
            return remove_monitor(topic)
        if action == "list":
            topics = list_monitors()
            return ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
        return {"ok": False, "error": "Specify action (add/remove/list) and a topic."}

    def run_save_memory(args: dict):
        return {"result": mm.remember(args.get("key", ""), args.get("value", ""), args.get("category", "notes")),
                "silent": True}

    def run_forget(args: dict):
        scope = str(args.get("scope", "")).lower()
        if scope == "last":
            return mm.forget_last(int(args.get("count") or 1))
        if scope == "match":
            return mm.forget_matching(args.get("query", ""), args.get("category", ""))
        if scope == "temporary":
            return mm.clear_temporary()
        if scope == "all":
            return mm.clear_all_memory()
        return {"ok": False, "error": "scope must be last, match, temporary or all."}

    async def run_self_check(args: dict):
        report = await asyncio.to_thread(diagnostics.run_all, registry)
        show("SELF-CHECK", diagnostics.format_report(report))
        problems = [c for c in report["checks"] if c["status"] != "ok"]
        return {"result": report["summary"], "issues": problems}

    def run_read_messages(args: dict):
        platform = args.get("platform") or "instagram"
        if str(args.get("action", "inbox")).lower() == "conversation":
            result = messaging.read_conversation(platform, args.get("name", ""), args.get("count") or 15)
            if result.get("ok"):
                show(f"{result['platform'].upper()} — {result['with']}", result["messages"])
        else:
            result = messaging.read_inbox(platform, args.get("count") or 10)
            if result.get("ok"):
                show(f"{result['platform'].upper()} — INBOX", result["summary"] + "\n\n" + result["conversations"])
        return result

    def run_status(args: dict):
        return {
            "desktop_task_running": live.desktop_running,
            "desktop_task_progress": live.desktop_progress[-5:],
            "awaiting_confirmation": [p.summary for p in registry.gate.pending()],
            "ai_models": llm.status(),   # which Gemini models are busy (cooling down) or retired
            "recent_actions": [{k: a.get(k) for k in ("time", "tool", "status", "result", "error")}
                               for a in recent_actions(5)],
        }

    handlers = {
        "open_app":          lambda a: open_app(parameters=a, player=ui),
        "weather_report":    lambda a: weather_action(parameters=a, player=ui),
        "browser_control":   lambda a: browser_control(parameters=a, player=ui),
        "system_command":    lambda a: system_command(parameters=a, player=ui),
        "file_controller":   lambda a: file_controller(parameters=a, player=ui),
        "send_message":      lambda a: send_message(parameters=a, player=ui),
        "find_contact":      lambda a: find_contact(parameters=a, player=ui),
        "read_messages":     run_read_messages,
        "reminder":          lambda a: reminder(parameters=a, player=ui),
        "youtube_video":     lambda a: youtube_video(parameters=a, player=ui),
        "computer_settings": lambda a: computer_settings(parameters=a, player=ui),
        "desktop_control":   lambda a: desktop_control(parameters=a, player=ui),
        "code_helper":       lambda a: code_helper(parameters=a, player=ui, speak=speak),
        "dev_agent":         lambda a: dev_agent(parameters=a, player=ui, speak=speak),
        "computer_control":  lambda a: computer_control(parameters=a, player=ui),
        "game_updater":      lambda a: game_updater(parameters=a, player=ui, speak=speak),
        "flight_finder":     lambda a: flight_finder(parameters=a, player=ui),
        "system_status":     lambda a: str(get_system_status()),
        "web_search":        run_web_search,
        "news_report":       run_news,
        "file_processor":    run_file_processor,
        "manage_monitor":    run_monitor,
        "save_memory":       run_save_memory,
        "search_memory":     lambda a: mm.search_conversation_history(a.get("query", ""), int(a.get("limit") or 5)),
        "memory_forget":     run_forget,
        "memory_list":       lambda a: mm.list_memory(a.get("category", "")),
        "self_check":        run_self_check,
        "assistant_status":  run_status,
        # Need the live session: implemented on JarvisLive.
        "desktop_task":      live.tool_desktop_task,
        "screen_process":    live.tool_screen_process,
        "close_camera":      live.tool_close_camera,
        "shutdown_jarvis":   live.tool_shutdown,
    }

    declarations = {d["name"]: d for d in TOOL_DECLARATIONS}
    missing = set(declarations) - set(handlers)
    unknown = set(handlers) - set(declarations)
    if missing or unknown:
        raise RuntimeError(f"Tool declarations and handlers differ. Missing handlers: {sorted(missing)}; "
                           f"handlers without declarations: {sorted(unknown)}")
    def check_recipient(args: dict) -> dict:
        # Resolve "Yuliyan Ivanov" to exactly one account *before* asking "send?".
        resolved = messaging.resolve_recipient(args.get("platform", ""), args.get("receiver", ""))
        if not resolved.get("ok"):
            return resolved
        return {"args": {**args, "receiver": resolved["receiver"]}} if resolved.get("receiver") else {}

    preflights = {"send_message": check_recipient}
    private_results = {"read_messages"}   # message contents never go into logs/actions.jsonl
    for name, declaration in declarations.items():
        registry.register(declaration, handlers[name], retry_safe=name in RETRY_SAFE,
                          preflight=preflights.get(name), log_result=name not in private_results)
    return registry
