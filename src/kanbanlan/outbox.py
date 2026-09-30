"""Local-first lifecycle changes: a per-repository outbox drained to GitHub.

A lifecycle command validates against the local view (the shared snapshot
with every pending change laid over it), records its change here as an
intent, and returns. A drainer then replays each intent, oldest first, as
the ordinary live command in a child process, which re-validates against
GitHub and performs every read and write the synchronous command always
did, including the post-claim verification that detects a claim lost to
another machine.

Rules the drainer keeps:

- One drainer per repository at a time (a non-blocking lock), and intents
  run strictly in creation order.
- A failed intent is never retried on its own: it stays in the outbox as
  failed, and every later intent for the same request fails as blocked
  rather than running on top of a state that never happened.
- An intent found "running" belonged to a drainer that died mid-command.
  Its effect on GitHub is unknown, so it is failed too, never replayed.
- Applied intents keep overlaying the snapshot until one refresh taken
  after them lands, so a read never flickers back to the old state.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kanbanlan.domain import KanbanlanRequest
from kanbanlan.locks import FileLock
from kanbanlan.snapshot import count_statuses, isoformat, select_ready, utc_now

QUEUED = "queued"
RUNNING = "running"
APPLIED = "applied"
FAILED = "failed"
PENDING_STATES = (QUEUED, RUNNING, APPLIED)

EXECUTOR_ENV = "KANBANLAN_SYNC_EXECUTOR"
WRITE_BEHIND_ENV = "KANBANLAN_WRITE_BEHIND"

# Enqueueing holds the arbitration lock only for a local read, a check, and
# one small file write (plus, for a claim, creating its worktree).
ARBITRATION_TIMEOUT_SECONDS = 30.0


def write_behind_enabled() -> bool:
    """Report whether lifecycle commands queue instead of waiting on GitHub.

    The drainer's own child commands always run live, or they would only
    queue themselves again.
    """

    if os.environ.get(EXECUTOR_ENV) == "1":
        return False
    return os.environ.get(WRITE_BEHIND_ENV, "1") != "0"


@dataclass
class Intent:
    id: str
    seq: int
    kind: str
    reference: str
    label: str
    argv: list[str]
    effect: dict[str, Any]
    kanbanlan_id: str | None = None
    created_at: str = ""
    state: str = QUEUED
    error: str | None = None
    finished_at: str | None = None
    note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        reference: str,
        label: str,
        argv: list[str],
        effect: dict[str, Any],
        kanbanlan_id: str | None,
        note: str | None = None,
    ) -> Intent:
        return cls(
            id=secrets.token_hex(6),
            seq=time.time_ns(),
            kind=kind,
            reference=reference,
            label=label,
            argv=argv,
            effect=effect,
            kanbanlan_id=kanbanlan_id,
            created_at=isoformat(utc_now()),
            note=note,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Intent:
        fields = {name: value[name] for name in cls.__dataclass_fields__ if name in value}
        return cls(**fields)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def filename(self) -> str:
        return f"{self.seq:020d}-{self.id}.json"


class Outbox:
    def __init__(self, cache_directory: Path):
        self.directory = cache_directory / "outbox"
        self.lock_path = cache_directory / "outbox.lock"
        self.drain_lock_path = cache_directory / "drain.lock"

    def arbitration(self) -> FileLock:
        """Serialize check-then-enqueue across every local session."""

        self._prepare()
        return FileLock(self.lock_path, timeout=ARBITRATION_TIMEOUT_SECONDS)

    def intents(self) -> list[Intent]:
        try:
            paths = sorted(self.directory.glob("*.json"))
        except OSError:
            return []
        values: list[Intent] = []
        for path in paths:
            try:
                values.append(Intent.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                continue
        return sorted(values, key=lambda value: value.seq)

    def pending(self) -> list[Intent]:
        return [value for value in self.intents() if value.state in PENDING_STATES]

    def failed(self) -> list[Intent]:
        return [value for value in self.intents() if value.state == FAILED]

    def find(self, prefix: str) -> Intent:
        matches = [value for value in self.intents() if value.id.startswith(prefix)]
        if not matches:
            raise RuntimeError(f"no queued change matches {prefix!r}")
        if len(matches) > 1:
            raise RuntimeError(f"change id {prefix!r} is ambiguous")
        return matches[0]

    def write(self, intent: Intent) -> None:
        self._prepare()
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".intent.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(intent.to_dict(), stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.directory / intent.filename)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def remove(self, intent: Intent) -> None:
        (self.directory / intent.filename).unlink(missing_ok=True)

    def blocking_failure(
        self, kanbanlan_id: str | None, before: int | None = None
    ) -> Intent | None:
        if not kanbanlan_id:
            return None
        for value in self.failed():
            if value.kanbanlan_id == kanbanlan_id and (before is None or value.seq < before):
                return value
        return None

    def _prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)


def pending_item(
    *,
    kanbanlan_id: str,
    title: str,
    body: str,
    priority: str,
    repository: str,
) -> dict[str, Any]:
    """Describe a captured request that GitHub has not numbered yet."""

    return {
        "type": "ISSUE",
        "kanbanlan_id": kanbanlan_id,
        "provider": "github",
        "provider_ref": kanbanlan_id,
        "display_id": "pending",
        "title": title,
        "body": body,
        "url": None,
        "canonical_url": None,
        "number": None,
        "repository": repository,
        "state": "OPEN",
        "status": "Inbox",
        "priority": priority,
        "labels": [{"name": priority, "color": ""}, {"name": "status:intake", "color": ""}],
        "active_claim": None,
        "session_history": [],
        "session_history_truncated": False,
        "linked_open_pull_requests": [],
    }


def overlay(snapshot: dict[str, Any], intents: list[Intent]) -> dict[str, Any]:
    """Return the snapshot as it will look once every pending intent lands.

    Every effect is idempotent, because an applied intent keeps overlaying
    a snapshot that may already reflect it.
    """

    view = copy.deepcopy(snapshot)
    items = view.setdefault("items", [])
    repository = view.get("source", {}).get("repository")
    for intent in intents:
        if intent.state not in PENDING_STATES:
            continue
        effect = intent.effect
        created = effect.get("create")
        if created:
            if not any(item.get("kanbanlan_id") == created["kanbanlan_id"] for item in items):
                items.append({**created, "pending_sync": [intent.id]})
            continue
        item = _find(items, intent, repository)
        if item is None:
            continue
        if "status" in effect:
            item["status"] = effect["status"]
        if "state" in effect:
            item["state"] = effect["state"]
        if "claim" in effect:
            item["active_claim"] = effect["claim"]
        item.setdefault("pending_sync", [])
        if intent.id not in item["pending_sync"]:
            item["pending_sync"].append(intent.id)
    if repository:
        ready = select_ready(items, repository)
        view["ready_cards"] = ready
        view["next_ready"] = ready[0] if ready else None
    view["status_counts"] = count_statuses(items)
    return view


def _find(
    items: list[dict[str, Any]], intent: Intent, repository: str | None
) -> dict[str, Any] | None:
    for item in items:
        if item.get("type") != "ISSUE":
            continue
        if intent.kanbanlan_id and item.get("kanbanlan_id") == intent.kanbanlan_id:
            return item
    for item in items:
        if item.get("type") == "ISSUE" and KanbanlanRequest.from_snapshot_item(item).matches(
            intent.reference, local_repository=repository
        ):
            return item
    return None


@dataclass(frozen=True)
class Execution:
    ok: bool
    error: str | None = None


def drain(
    outbox: Outbox,
    *,
    execute: Callable[[Intent], Execution],
    refresh: Callable[[], Any],
    wait: float = 0.0,
) -> bool:
    """Run queued intents in order; return False when another drainer holds the queue.

    After releasing the queue the drainer looks once more, because a
    session may have enqueued, and found the lock held, just as this
    drainer was finishing; without the second look that intent would wait
    for the next drain.
    """

    drained = False
    while True:
        try:
            lock = FileLock(outbox.drain_lock_path, timeout=wait)
            outbox._prepare()
            lock.__enter__()
        except RuntimeError:
            return drained
        try:
            _drain_locked(outbox, execute=execute, refresh=refresh)
            drained = True
        finally:
            lock.__exit__(None, None, None)
        if not any(value.state == QUEUED for value in outbox.intents()):
            return drained


def _drain_locked(
    outbox: Outbox,
    *,
    execute: Callable[[Intent], Execution],
    refresh: Callable[[], Any],
) -> None:
    for intent in outbox.intents():
        if intent.state == RUNNING:
            _finish(
                outbox,
                intent,
                FAILED,
                "interrupted while syncing; its effect on GitHub is unknown. Check the "
                "board, then retry or dismiss it with 'kanbanlan sync'",
            )
    while True:
        queued = [value for value in outbox.intents() if value.state == QUEUED]
        if not queued:
            break
        intent = queued[0]
        blocker = outbox.blocking_failure(intent.kanbanlan_id, before=intent.seq)
        if blocker is not None:
            _finish(
                outbox,
                intent,
                FAILED,
                f"not attempted: the earlier {blocker.kind} of this request failed",
            )
            continue
        intent.state = RUNNING
        outbox.write(intent)
        try:
            result = execute(intent)
        except Exception as exc:  # the drainer must record, not crash, on any failure
            result = Execution(False, str(exc))
        if result.ok:
            _finish(outbox, intent, APPLIED, None)
        else:
            _finish(outbox, intent, FAILED, result.error or "failed")
    applied = [value for value in outbox.intents() if value.state == APPLIED]
    if applied:
        # A failed refresh leaves them applied; the next drain refreshes again.
        refresh()
        for intent in applied:
            outbox.remove(intent)


def _finish(outbox: Outbox, intent: Intent, state: str, error: str | None) -> None:
    intent.state = state
    intent.error = error
    intent.finished_at = isoformat(utc_now())
    outbox.write(intent)


def execute_intent(root: Path) -> Callable[[Intent], Execution]:
    def execute(intent: Intent) -> Execution:
        env = os.environ.copy()
        env[EXECUTOR_ENV] = "1"
        env["KANBANLAN_BACKGROUND_REFRESH"] = "skip"
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "kanbanlan", "-C", str(root), "--json", *intent.argv],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Execution(False, "timed out after 600 seconds")
        if completed.returncode == 0:
            return Execution(True)
        return Execution(False, _executor_error(completed.stderr, completed.stdout))

    return execute


def _executor_error(stderr: str, stdout: str) -> str:
    """Return the failed child command's own error message.

    ``--json`` errors are one indented JSON document, possibly after other
    output, so the parse starts at every line that opens an object rather
    than trusting any single line.
    """

    decoder = json.JSONDecoder()
    for stream in (stderr, stdout):
        text = stream.strip()
        starts = [index for index, char in enumerate(text) if char == "{"]
        for start in reversed(starts):
            if start and text[start - 1] != "\n":
                continue
            try:
                payload, _ = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                continue
            error = payload.get("error") if isinstance(payload, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if message:
                hint = error.get("hint")
                return f"{message} ({hint})" if hint else message
    detail = (stderr.strip() or stdout.strip()).splitlines()
    return detail[-1] if detail else "failed without output"


def drain_outbox(root: Path, store: Any, provider: Any, *, wait: float = 0.0) -> bool:
    """Drain one repository's outbox, replaying each change as a live command."""

    return drain(
        Outbox(store.directory),
        execute=execute_intent(root),
        refresh=lambda: store.refresh_for_write(provider),
        wait=wait,
    )
