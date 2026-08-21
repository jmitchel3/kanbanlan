"""Shared owner-verified file lock primitives.

Every inter-process lock in Kanbanlan records its owner's PID and removes
lock state only when that recorded owner is provably gone, verified through
``(st_dev, st_ino)`` file identity so a concurrent replacement is never
deleted by mistake. Age-of-file heuristics alone cannot distinguish a
crashed holder from a live but slow one, which is why an mtime check
survives here only as the last resort for a lock file with no readable PID.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# A lock file whose owner record cannot be read carries no liveness signal,
# so age is the only thing left to reason from. One minute comfortably
# exceeds the moment between creating the file and writing the record.
UNREADABLE_LOCK_STALE_SECONDS = 60.0


def file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def unlink_if_unchanged(path: Path, identity: tuple[int, int] | None) -> None:
    """Remove ``path`` only while it is still the exact file ``identity`` names.

    Between deciding a lock is stale and unlinking it, another process may
    have removed it and created its own; deleting blindly would steal that
    new owner's lock.
    """

    if identity is None or file_identity(path) != identity:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def lock_pid(path: Path) -> int | None:
    """Read the owner PID from a lock file, or None when there is none to read.

    Accepts both the JSON owner record written here and a bare PID left by an
    older Kanbanlan, so an upgrade never mistakes a live legacy holder for an
    unreadable lock.
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if isinstance(value, dict):
        value = value.get("pid", 0)
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def write_owner_record(descriptor: int) -> tuple[int, int]:
    """Record this process as the lock owner and return the file's identity."""

    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = (json.dumps({"pid": os.getpid(), "started_at": started_at}) + "\n").encode()
    if os.write(descriptor, payload) != len(payload):
        raise OSError("could not write the complete lock owner record")
    os.fsync(descriptor)
    stat = os.fstat(descriptor)
    return stat.st_dev, stat.st_ino


class FileLock:
    """Blocking inter-process lock that never steals from a live owner.

    Waiters queue behind the recorded owner for at most ``timeout`` seconds
    and then fail loudly; they take over only when that owner's PID is
    provably dead, however old the lock file has grown, because a slow
    refresh holding the lock is exactly the case the lock exists to protect.
    Release verifies file identity first, so a holder that somehow lost the
    lock cannot delete its successor's.

    The default timeout allows for a project-scope snapshot refresh, which
    paginates the whole Project and then queries pull requests per
    repository with retries, legitimately running well past ten seconds.
    """

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self.identity: tuple[int, int] | None = None

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                identity = file_identity(self.path)
                pid = lock_pid(self.path)
                if pid is not None and not pid_running(pid):
                    unlink_if_unchanged(self.path, identity)
                    continue
                if pid is None and self._unreadable_and_stale():
                    unlink_if_unchanged(self.path, identity)
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"timed out waiting for lock {self.path}")
                time.sleep(0.05)
                continue
            try:
                self.identity = write_owner_record(descriptor)
            except Exception:
                os.close(descriptor)
                unlink_if_unchanged(self.path, file_identity(self.path))
                raise
            os.close(descriptor)
            return self

    def __exit__(self, *_args: Any) -> None:
        if self.identity is not None:
            unlink_if_unchanged(self.path, self.identity)
            self.identity = None

    def _unreadable_and_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age > UNREADABLE_LOCK_STALE_SECONDS
