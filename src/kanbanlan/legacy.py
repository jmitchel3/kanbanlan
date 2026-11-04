"""Migration paths for the removed background worker and write-behind outbox.

Kanbanlan used to queue lifecycle changes in a per-repository outbox and
keep a user-level worker process refreshing every registered repository.
Both are gone: lifecycle commands now write GitHub directly. This module
only cleans up what an older version left behind:

- ``uninstall_worker`` unloads and removes the documented launchd or
  systemd unit, stops a worker process that is still running, and deletes
  the worker's registry and lock files.
- ``apply_legacy_outbox`` replays changes still queued in an old outbox
  once, in creation order, reports every change it could not apply, and
  then removes the outbox. A queued change is never dropped without a
  report.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kanbanlan.config import state_dir
from kanbanlan.locks import FileLock, lock_pid, owner_predates_lock, pid_running

LAUNCHD_LABEL = "com.kanbanlan.worker"
SYSTEMD_UNIT = "kanbanlan-worker.service"
# Everything the worker kept in the user state directory. Account bindings
# (accounts.json) live there too and are kept.
# 'worker start' also wrote the detached worker's output to worker.log.
WORKER_STATE_FILES = (
    "registry.json",
    "registry.lock",
    "worker.lock",
    "worker.lock.steal",
    "worker.log",
)

# Set for the child command that replays one legacy change, so it does not
# try to migrate the outbox its parent is already migrating.
SKIP_ENV = "KANBANLAN_SKIP_LEGACY_OUTBOX"
REPLAY_TIMEOUT_SECONDS = 600
# Another session migrating the same outbox finishes within its replays.
MIGRATION_LOCK_TIMEOUT_SECONDS = REPLAY_TIMEOUT_SECONDS + 30.0

QUEUED = "queued"
WAITING = "waiting"
RUNNING = "running"
APPLIED = "applied"
FAILED = "failed"

Runner = Callable[..., Any]


def launchd_unit() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "systemd" / "user" / SYSTEMD_UNIT


def installed_units() -> list[Path]:
    """Return the worker service units still installed for this user."""

    return [unit for unit in (launchd_unit(), systemd_unit()) if unit.exists()]


def running_worker(directory: Path | None = None) -> int | None:
    """Return the PID of a worker process still holding its lock, if any."""

    lock = (directory or state_dir()) / "worker.lock"
    pid = lock_pid(lock)
    if pid is not None and pid_running(pid) and owner_predates_lock(lock, pid):
        return pid
    return None


def worker_leftovers(directory: Path | None = None) -> dict[str, Any]:
    """Describe what an older version's worker left installed or running."""

    directory = directory or state_dir()
    return {
        "units": [str(unit) for unit in installed_units()],
        "pid": running_worker(directory),
        "state_files": [
            str(directory / name) for name in WORKER_STATE_FILES if (directory / name).exists()
        ],
    }


def _quiet(run: Runner, command: list[str]) -> None:
    try:
        run(command, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        # The service manager is missing or hung; the unit file still goes.
        pass


def uninstall_worker(
    *,
    run: Runner = subprocess.run,
    directory: Path | None = None,
    stop_timeout: float = 5.0,
) -> dict[str, Any]:
    """Unload and remove the worker's units, stop it, and delete its state."""

    directory = directory or state_dir()
    removed_units: list[str] = []
    for unit in installed_units():
        if unit.suffix == ".plist":
            # Unload first: launchd restarts a KeepAlive job that is killed.
            _quiet(run, ["launchctl", "bootout", f"gui/{os.getuid()}", str(unit)])
            unit.unlink(missing_ok=True)
        else:
            _quiet(run, ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
            unit.unlink(missing_ok=True)
            _quiet(run, ["systemctl", "--user", "daemon-reload"])
        removed_units.append(str(unit))
    stopped = running_worker(directory)
    if stopped is not None:
        try:
            os.kill(stopped, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + stop_timeout
        while pid_running(stopped) and time.monotonic() < deadline:
            time.sleep(0.05)
        if pid_running(stopped):
            raise RuntimeError(f"worker process {stopped} did not stop after SIGTERM")
    removed_state: list[str] = []
    for name in WORKER_STATE_FILES:
        path = directory / name
        if path.exists():
            path.unlink(missing_ok=True)
            removed_state.append(str(path))
    return {
        "units_removed": removed_units,
        "stopped_pid": stopped,
        "state_removed": removed_state,
    }


def legacy_outbox(cache_directory: Path) -> Path:
    return cache_directory / "outbox"


def pending_legacy_changes(cache_directory: Path) -> int:
    """Count the changes an old outbox still holds (any state but applied)."""

    count = 0
    for path in _intent_paths(legacy_outbox(cache_directory)):
        value = _read_intent(path)
        if value is None or value.get("state") != APPLIED:
            count += 1
    return count


def apply_legacy_outbox(
    root: Path,
    cache_directory: Path,
    *,
    execute: Callable[[list[str]], tuple[bool, str | None]] | None = None,
) -> list[dict[str, Any]]:
    """Replay an old outbox once, then remove it; return what happened to each change.

    Queued changes (and ones that waited out a quota reset) run in creation
    order as the live command. A change that already failed, or was
    interrupted mid-sync with an unknown effect, is not run again; it is
    reported with the command that would retry it. So is every later change
    to a request whose earlier change failed. Changes already applied need
    nothing and are not reported.
    """

    directory = legacy_outbox(cache_directory)
    if os.environ.get(SKIP_ENV) == "1" or not directory.is_dir():
        return []
    execute = execute or _replay(root)
    report: list[dict[str, Any]] = []
    # The old version enqueued under outbox.lock and drained under
    # drain.lock; holding both means no older session still running on this
    # machine can add or apply a change while the outbox is migrated.
    with (
        FileLock(cache_directory / "outbox.lock", timeout=MIGRATION_LOCK_TIMEOUT_SECONDS),
        FileLock(cache_directory / "drain.lock", timeout=MIGRATION_LOCK_TIMEOUT_SECONDS),
    ):
        if not directory.is_dir():
            # Another session migrated it while this one waited.
            return []
        failed_requests: set[str] = set()
        values = [(path, _read_intent(path)) for path in _intent_paths(directory)]
        values.sort(key=lambda entry: (entry[1] or {}).get("seq") or 0)
        for path, value in values:
            if value is None:
                report.append(
                    {"file": str(path), "outcome": "unreadable", "command": None, "error": None}
                )
                continue
            state = value.get("state")
            if state == APPLIED:
                continue
            argv = [str(part) for part in value.get("argv") or []]
            entry = {
                "id": value.get("id"),
                "kind": value.get("kind"),
                "request": value.get("label") or value.get("reference"),
                "command": ["kanbanlan", *argv] if argv else None,
                "error": value.get("error"),
            }
            # A change queued before its request had an ID names it by provider ref.
            request = value.get("kanbanlan_id") or value.get("reference")
            if state in (QUEUED, WAITING) and argv and request not in failed_requests:
                ok, message = execute(argv)
                entry["outcome"] = "applied" if ok else "failed"
                entry["error"] = None if ok else message
            elif state == RUNNING:
                entry["outcome"] = "not applied"
                entry["error"] = "interrupted while syncing; its effect on GitHub is unknown"
            elif state in (QUEUED, WAITING) and argv:
                entry["outcome"] = "not applied"
                entry["error"] = "an earlier change to this request failed"
            else:
                entry["outcome"] = "not applied"
            if entry["outcome"] != "applied" and request:
                failed_requests.add(request)
            report.append(entry)
        shutil.rmtree(directory, ignore_errors=True)
    return report


def describe_change(entry: dict[str, Any]) -> str:
    """One line for a migrated change, naming the command that retries it."""

    if entry["outcome"] == "unreadable":
        return f"legacy queued change {entry['file']} could not be read and was not applied"
    head = f"legacy queued {entry.get('kind')} of {entry.get('request')}"
    if entry["outcome"] == "applied":
        return f"{head} was applied to GitHub"
    detail = f" ({entry['error']})" if entry.get("error") else ""
    command = entry.get("command")
    retry = f"; to apply it, run: {shlex.join(command)}" if command else ""
    return f"{head} was not applied{detail}{retry}"


def _intent_paths(directory: Path) -> list[Path]:
    try:
        return sorted(directory.glob("*.json"))
    except OSError:
        return []


def _read_intent(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _replay(root: Path) -> Callable[[list[str]], tuple[bool, str | None]]:
    def execute(argv: list[str]) -> tuple[bool, str | None]:
        env = {**os.environ, SKIP_ENV: "1"}
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "kanbanlan", "-C", str(root), "--json", *argv],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=REPLAY_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"timed out after {REPLAY_TIMEOUT_SECONDS} seconds"
        if completed.returncode == 0:
            return True, None
        return False, _error_message(completed.stderr, completed.stdout)

    return execute


def _error_message(stderr: str, stdout: str) -> str:
    for text in (stderr, stdout):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    lines = (stderr.strip() or stdout.strip()).splitlines()
    return lines[-1] if lines else "failed without output"
