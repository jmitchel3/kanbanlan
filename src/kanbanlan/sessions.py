from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SESSION_ACTIVITY_SCHEMA = 1
SESSION_ACTIVITY_RE = re.compile(r"<!-- kanbanlan:session-activity (?P<payload>\{[^\n]*\}) -->")
HARNESS_RE = re.compile(r"^[a-z][a-z0-9-]*$")

RESUME_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "resume", "{session_id}"),
    "claude": ("claude", "--resume", "{session_id}"),
    "grok": ("grok", "--resume", "{session_id}"),
    "agy": ("agy", "--conversation", "{session_id}"),
}
HARNESS_ALIASES = {
    "antigravity": "agy",
    "claude-code": "claude",
    "google-antigravity": "agy",
    "grok-build": "grok",
}
NATIVE_SESSION_ENV = (
    ("CODEX_THREAD_ID", "codex"),
    ("CLAUDE_SESSION_ID", "claude"),
    ("GROK_SESSION_ID", "grok"),
    ("AGY_CONVERSATION_ID", "agy"),
)


@dataclass(frozen=True)
class AgentSession:
    harness: str
    session_id: str
    source: str

    def __post_init__(self) -> None:
        harness = normalize_harness(self.harness)
        session_id = self.session_id.strip()
        if not HARNESS_RE.fullmatch(harness):
            raise ValueError(f"invalid agent harness: {self.harness!r}")
        if (
            not session_id
            or session_id.startswith("-")
            or len(session_id) > 512
            or any(ord(value) < 32 or ord(value) == 127 for value in session_id)
            or any(value in session_id for value in ("<!--", "-->"))
        ):
            raise ValueError("agent session ID must be a safe, non-empty single-line value")
        object.__setattr__(self, "harness", harness)
        object.__setattr__(self, "session_id", session_id)

    @property
    def reference(self) -> str:
        return f"{self.harness}:{self.session_id}"

    @property
    def display(self) -> str:
        return f"{self.session_id} · {self.harness}"

    @property
    def resume_command(self) -> tuple[str, ...] | None:
        template = RESUME_COMMANDS.get(self.harness)
        if template is None:
            return None
        return tuple(value.format(session_id=self.session_id) for value in template)

    def to_dict(self) -> dict[str, Any]:
        command = self.resume_command
        return {
            "harness": self.harness,
            "session_id": self.session_id,
            "display": self.display,
            "reference": self.reference,
            "resumable": command is not None,
            "resume_command": list(command) if command else None,
            "source": self.source,
        }


def normalize_harness(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    return HARNESS_ALIASES.get(normalized, normalized)


def parse_agent_session(value: str, *, source: str = "explicit") -> AgentSession:
    raw = value.strip()
    if " · " in raw:
        session_id, harness = raw.rsplit(" · ", 1)
    elif ":" in raw:
        harness, session_id = raw.split(":", 1)
    else:
        raise ValueError("agent session must use HARNESS:SESSION_ID format")
    return AgentSession(harness=harness, session_id=session_id, source=source)


def responsible_session(
    action: str,
    actor: AgentSession | None,
    owner_session: str | None,
) -> AgentSession | None:
    if action in {"claim", "handoff"} and owner_session:
        try:
            return parse_agent_session(owner_session, source="activity owner")
        except ValueError:
            pass
    return actor


def detect_agent_session(
    *,
    explicit: str | None = None,
    environ: Mapping[str, str] | None = None,
    store: SessionContextStore | None = None,
    root: Path | None = None,
) -> AgentSession | None:
    values = environ if environ is not None else os.environ
    if explicit:
        return parse_agent_session(explicit, source="cli")
    if values.get("KANBANLAN_AGENT_SESSION"):
        return parse_agent_session(
            values["KANBANLAN_AGENT_SESSION"],
            source="KANBANLAN_AGENT_SESSION",
        )
    harness = values.get("KANBANLAN_AGENT")
    session_id = values.get("KANBANLAN_SESSION_ID")
    if bool(harness) != bool(session_id):
        raise ValueError("KANBANLAN_AGENT and KANBANLAN_SESSION_ID must be set together")
    if harness and session_id:
        return AgentSession(harness, session_id, "kanbanlan environment")
    for variable, native_harness in NATIVE_SESSION_ENV:
        if values.get(variable):
            return AgentSession(native_harness, values[variable], variable)
    if store is not None and root is not None:
        return store.resolve(root, environ=values)
    return None


class SessionContextStore:
    """Private hook-fed session context shared by a repository's worktrees."""

    def __init__(self, cache_root: Path):
        self.path = cache_root / "agent-sessions.json"
        self.lock_path = cache_root / "agent-sessions.lock"

    def register(
        self,
        session: AgentSession,
        *,
        workspaces: list[str],
        cwd: str | None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        values = environ if environ is not None else os.environ
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        record = {
            "harness": session.harness,
            "session_id": session.session_id,
            "registered_at": now,
            "workspaces": sorted({str(Path(value).resolve()) for value in workspaces if value}),
            "cwd": str(Path(cwd).resolve()) if cwd else None,
            "terminal_session_id": values.get("TERM_SESSION_ID"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _ExclusiveLock(self.lock_path):
            payload = self._read()
            contexts = [
                value
                for value in payload.get("contexts", [])
                if not (
                    value.get("harness") == session.harness
                    and value.get("session_id") == session.session_id
                )
            ]
            contexts.append(record)
            contexts = sorted(
                contexts,
                key=lambda value: value.get("registered_at", ""),
            )[-64:]
            self._write({"schema_version": 1, "contexts": contexts})

    def resolve(
        self,
        root: Path,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> AgentSession | None:
        values = environ if environ is not None else os.environ
        resolved_root = root.resolve()
        cutoff = time.time() - 24 * 60 * 60
        candidates: list[dict[str, Any]] = []
        for value in self._read().get("contexts", []):
            try:
                registered = datetime.fromisoformat(
                    str(value["registered_at"]).replace("Z", "+00:00")
                ).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            if registered < cutoff:
                continue
            candidates.append(value)

        terminal_session_id = values.get("TERM_SESSION_ID")
        matched_terminal = False
        terminal_aware = [value for value in candidates if value.get("terminal_session_id")]
        if terminal_session_id and terminal_aware:
            terminal_matches = [
                value
                for value in terminal_aware
                if value.get("terminal_session_id") == terminal_session_id
            ]
            if not terminal_matches:
                return None
            candidates = terminal_matches
            matched_terminal = True
        if not matched_terminal:
            path_matches = [
                value
                for value in candidates
                if any(
                    _paths_overlap(resolved_root, Path(path))
                    for path in [value.get("cwd"), *value.get("workspaces", [])]
                    if path
                )
            ]
            if path_matches:
                candidates = path_matches

        unique = {
            (str(value.get("harness")), str(value.get("session_id")))
            for value in candidates
            if value.get("harness") and value.get("session_id")
        }
        if len(unique) != 1:
            return None
        harness, session_id = unique.pop()
        return AgentSession(harness, session_id, "agent lifecycle hook")

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {"schema_version": 1, "contexts": []}
        return value if isinstance(value, dict) else {"schema_version": 1, "contexts": []}

    def _write(self, value: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def session_from_hook_payload(payload: dict[str, Any], harness: str) -> AgentSession:
    normalized = normalize_harness(harness)
    candidates = (
        payload.get("session_id"),
        payload.get("sessionId"),
        payload.get("conversationId"),
        payload.get("thread-id"),
    )
    session_id = next((str(value) for value in candidates if value), None)
    if not session_id:
        raise ValueError("hook payload does not contain a supported session identifier")
    return AgentSession(normalized, session_id, "agent lifecycle hook")


def hook_workspaces(payload: dict[str, Any]) -> list[str]:
    candidates = payload.get("workspacePaths") or payload.get("workspace_roots") or []
    if isinstance(candidates, str):
        return [candidates]
    if isinstance(candidates, list):
        return [str(value) for value in candidates if value]
    return []


def activity_comment(
    *,
    action: str,
    at: str,
    from_status: str | None,
    to_status: str | None,
    actor: AgentSession | None,
    owner_session: str | None = None,
) -> str:
    responsible = responsible_session(action, actor, owner_session)
    payload: dict[str, Any] = {
        "schema_version": SESSION_ACTIVITY_SCHEMA,
        "action": action,
        "at": at,
        "from_status": from_status,
        "to_status": to_status,
        "actor": actor.to_dict() if actor else None,
        "responsible": responsible.to_dict() if responsible else None,
        "owner_session": owner_session,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    transition = ""
    if from_status or to_status:
        transition = f" ({from_status or 'none'} -> {to_status or 'none'})"
    responsible_display = responsible.display if responsible else "unavailable"
    lines = [
        f"KANBANLAN ACTIVITY: {action}{transition}",
        f"Agent session: {responsible_display}",
    ]
    if actor and (not responsible or actor.reference != responsible.reference):
        lines.append(f"Actor session: {actor.display}")
    lines.append(f"<!-- kanbanlan:session-activity {encoded} -->")
    return "\n".join(lines)


def session_history(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for comment in sorted(comments, key=lambda value: value.get("createdAt", "")):
        body = comment.get("body") or ""
        match = SESSION_ACTIVITY_RE.search(body)
        if not match:
            continue
        try:
            payload = json.loads(match.group("payload"))
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != SESSION_ACTIVITY_SCHEMA
            or not isinstance(payload.get("action"), str)
        ):
            continue
        actor = _session_from_activity(payload.get("actor"))
        responsible = _session_from_activity(payload.get("responsible"))
        if responsible is None:
            responsible = responsible_session(
                str(payload["action"]),
                actor,
                payload.get("owner_session"),
            )
        payload["actor"] = actor.to_dict() if actor else None
        payload["responsible"] = responsible.to_dict() if responsible else None
        payload["commented_at"] = comment.get("createdAt")
        payload["author"] = (comment.get("author") or {}).get("login")
        history.append(payload)
    return history


def _session_from_activity(value: Any) -> AgentSession | None:
    if not isinstance(value, dict):
        return None
    try:
        return AgentSession(
            str(value["harness"]),
            str(value["session_id"]),
            str(value.get("source") or "activity"),
        )
    except (KeyError, TypeError, ValueError):
        return None


class _ExclusiveLock:
    def __init__(self, path: Path, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout

    def __enter__(self) -> _ExclusiveLock:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 60:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"timed out waiting for session context lock {self.path}")
                time.sleep(0.05)

    def __exit__(self, *_args: object) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        resolved_second = second.resolve()
    except OSError:
        return False
    return (
        resolved_second == first
        or resolved_second in first.parents
        or first in resolved_second.parents
    )
