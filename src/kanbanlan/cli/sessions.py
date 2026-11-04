"""Agent session commands: sessions, resume, and session-hook."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from typing import Any

from kanbanlan.cli.cards import _issue, _local_view
from kanbanlan.cli.context import _context, _root
from kanbanlan.cli.output import _emit_result
from kanbanlan.config import Config, cache_dir
from kanbanlan.domain import request_label
from kanbanlan.sessions import SessionContextStore, hook_workspaces, session_from_hook_payload
from kanbanlan.ui import warning


def _cmd_sessions(args: argparse.Namespace) -> int:
    root, _, provider, store = _context(args)
    snapshot = _local_view(
        root, store, provider, label=f"Loading session activity for request {args.request}"
    )
    item = _issue(snapshot, args.request)
    history = _session_activity(item, action=args.action)
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "provider_ref": item.get("provider_ref"),
        "history_truncated": item.get("session_history_truncated", False),
        "events": history,
    }
    if _emit_result(args, result):
        return 0
    if not history:
        print("No matching agent session activity is recorded.")
        return 0
    for event in history:
        actor = event.get("actor")
        responsible = event.get("responsible") or actor
        display = responsible.get("display") if responsible else "unavailable"
        destination = event.get("to_status") or "no status change"
        print(f"{event['action']:<10} {destination:<12} {display}  {event.get('at') or ''}")
        command = responsible.get("resume_command") if responsible else None
        if command:
            print(f"  resume: {shlex.join(command)}")
        if actor and responsible and actor.get("reference") != responsible.get("reference"):
            print(f"  actor:  {actor.get('display')}")
    if item.get("session_history_truncated"):
        warning("Only the most recent 100 request comments were available; history may be partial.")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    root, _, provider, store = _context(args)
    snapshot = _local_view(
        root, store, provider, label=f"Loading resumable sessions for request {args.request}"
    )
    item = _issue(snapshot, args.request)
    history = _session_activity(item, action=args.action)
    resumable: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for event in history:
        responsible = event.get("responsible") or event.get("actor")
        if responsible and responsible.get("resume_command"):
            resumable.append((event, responsible))
    if not resumable:
        scope = f" for action {args.action!r}" if args.action else ""
        raise RuntimeError(f"request {request_label(item)} has no resumable agent session{scope}")
    selected, selected_session = resumable[-1]
    command = [str(value) for value in selected_session["resume_command"]]
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "action": selected["action"],
        "at": selected.get("at"),
        "session": selected_session,
        "actor_session": selected.get("actor"),
        "command": command,
    }
    if args.json_output:
        _emit_result(args, result)
        return 0
    if args.run:
        os.execvp(command[0], command)
        raise AssertionError("os.execvp returned unexpectedly")
    print(shlex.join(command))
    return 0


def _cmd_session_hook(args: argparse.Namespace) -> int:
    root = _root(args)
    config = Config.load(root)
    try:
        payload = json.load(sys.stdin)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not parse agent hook JSON from stdin: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("agent hook input must be a JSON object")
    if config.session_tracking_enabled():
        harness = "grok" if os.environ.get("GROK_SESSION_ID") else args.agent
        session = session_from_hook_payload(payload, harness)
        workspaces = hook_workspaces(payload)
        cwd = payload.get("cwd")
        if not cwd and workspaces:
            cwd = workspaces[0]
        SessionContextStore(cache_dir(root)).register(
            session,
            workspaces=workspaces or [str(root)],
            cwd=str(cwd or root),
        )
    print("{}")
    return 0


def _session_activity(
    item: dict[str, Any],
    *,
    action: str | None,
) -> list[dict[str, Any]]:
    history = item.get("session_history", [])
    if action:
        return [value for value in history if value.get("action") == action]
    return list(history)
