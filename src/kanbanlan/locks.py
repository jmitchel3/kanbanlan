"""Shared owner-verified file lock primitives.

Every inter-process lock in Kanbanlan records its owner's PID and removes
lock state only when that recorded owner is provably gone. Removal re-reads
the lock file's content besides checking ``(st_dev, st_ino)`` identity,
because a replacement file created right after an unlink can be assigned
the recycled inode, so a concurrent replacement is never deleted by
mistake; the removal itself is serialized through a sidecar guard file so
two waiters cannot interleave their steals. Age-of-file heuristics alone
cannot distinguish a crashed holder from a live but slow one, which is why
an mtime check survives here only as the last resort for a lock file with
no readable PID.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# A lock file whose owner record cannot be read carries no liveness signal,
# so age is the only thing left to reason from. One minute comfortably
# exceeds the moment between creating the file and writing the record.
UNREADABLE_LOCK_STALE_SECONDS = 60.0

# A steal guard is held only for a few stat, read, and unlink calls, so a
# guard this old can only belong to a stealer that crashed mid-steal.
STEAL_GUARD_STALE_SECONDS = 5.0

# Integer process ages and clock adjustments can make a legitimate owner
# look marginally younger than its lock; PID recycling gaps are far larger.
RECYCLED_PID_SLACK_SECONDS = 30.0

# When the owner's age cannot be read at all, its PID is honored, but only
# up to this lock-file age. Beyond it, a leftover lock (for example from
# before a reboot, its PID since recycled by an unrelated long-lived
# process) would otherwise starve every future session forever.
UNVERIFIABLE_OWNER_CAP_SECONDS = 6 * 3600.0


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


def read_owner_record(path: Path) -> dict[str, Any] | None:
    """Read a lock file's owner record, or None when there is none to read.

    Accepts both the JSON owner record written here and a bare PID left by an
    older Kanbanlan, so an upgrade never mistakes a live legacy holder for an
    unreadable lock. Any other JSON scalar is rejected rather than coerced:
    mapping ``true`` to PID 1 would name a process that never exits.
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return {"pid": value} if value > 0 else None


def lock_pid(path: Path) -> int | None:
    record = read_owner_record(path)
    if record is None:
        return None
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
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


def process_elapsed_seconds(pid: int) -> float | None:
    """Return how long process ``pid`` has been running, or None when unknown.

    ``ps -o etimes=`` is POSIX and reports whole seconds since the process
    started, which is what distinguishes a lock's original owner from an
    unrelated process that inherited its PID after a reboot.
    """

    try:
        result = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    fields = result.stdout.strip().split()
    if not fields:
        return None
    try:
        return float(fields[0])
    except ValueError:
        return None


def owner_predates_lock(path: Path, pid: int) -> bool:
    """Decide whether running process ``pid`` is old enough to own this lock.

    A lock file survives a reboot, and its recorded PID can then name an
    unrelated process, so a running PID alone is not proof of ownership:
    the owner necessarily started before it wrote the lock, so a process
    younger than the lock inherited a recycled PID and the lock is stale.
    When the process's age cannot be read, the PID is honored up to
    ``UNVERIFIABLE_OWNER_CAP_SECONDS`` of lock-file age, restoring the old
    self-healing valve without ever preempting a verifiably live owner.
    """

    try:
        lock_age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return True
    elapsed = process_elapsed_seconds(pid)
    if elapsed is not None:
        return elapsed + RECYCLED_PID_SLACK_SECONDS >= lock_age
    return lock_age <= UNVERIFIABLE_OWNER_CAP_SECONDS


def write_owner_record(descriptor: int) -> tuple[dict[str, Any], tuple[int, int]]:
    """Record this process as the lock owner; return the record and identity.

    The record carries a random nonce besides the PID because neither datum
    alone proves ownership at release time: a filesystem may hand a
    replacement file the recycled inode of the one just unlinked, and a PID
    can recur, but the nonce is unique to this acquisition.
    """

    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    record: dict[str, Any] = {
        "pid": os.getpid(),
        "nonce": secrets.token_hex(16),
        "started_at": started_at,
    }
    payload = (json.dumps(record) + "\n").encode()
    if os.write(descriptor, payload) != len(payload):
        raise OSError("could not write the complete lock owner record")
    os.fsync(descriptor)
    stat = os.fstat(descriptor)
    return record, (stat.st_dev, stat.st_ino)


def remove_stale_lock(path: Path, identity: tuple[int, int] | None, pid: int | None) -> None:
    """Remove a lock whose recorded owner was just judged to be gone.

    ``pid`` is the stale owner that was observed, or None for a lock whose
    record was unreadable. The whole verify-and-unlink sequence runs under
    a sidecar guard file so two waiters cannot interleave their steals:
    without it, a second waiter could complete its own steal and win the
    lock between this waiter's checks and its unlink, and the unlink would
    then delete the new live holder's lock. Inside the guard the content is
    re-read as well as the identity, because a replacement created moments
    after an unlink can be assigned the recycled inode. A waiter that finds
    the guard taken declines and lets its caller loop and retry.
    """

    guard = path.with_name(path.name + ".steal")
    try:
        descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Another stealer is at work; let it finish. A guard this old can
        # only belong to a stealer that crashed mid-steal, so sweep it and
        # let the caller's next retry proceed instead of wedging everyone.
        try:
            crashed = time.time() - guard.stat().st_mtime > STEAL_GUARD_STALE_SECONDS
        except FileNotFoundError:
            return
        if crashed:
            try:
                guard.unlink()
            except FileNotFoundError:
                pass
        else:
            time.sleep(0.01)
        return
    except FileNotFoundError:
        return
    os.close(descriptor)
    try:
        if identity is None or file_identity(path) != identity:
            return
        if lock_pid(path) != pid:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    finally:
        try:
            guard.unlink()
        except FileNotFoundError:
            pass


def release_owner_record(
    path: Path,
    record: dict[str, Any],
    identity: tuple[int, int] | None,
) -> None:
    """Remove the lock only while it still holds this exact owner record.

    The nonce comparison is what authorizes removal; the identity check is
    only a cheap first pass. Unreadable content means the lock is no longer
    ours to remove.
    """

    if identity is not None and file_identity(path) != identity:
        return
    current = read_owner_record(path)
    if current is None:
        return
    if current.get("pid") != record.get("pid") or current.get("nonce") != record.get("nonce"):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class FileLock:
    """Blocking inter-process lock that never steals from a live owner.

    Waiters queue behind the recorded owner for at most ``timeout`` seconds
    and then fail loudly; they take over only when that owner is provably
    gone (its PID dead, or its PID recycled by a process younger than the
    lock), however old the lock file has grown, because a slow refresh
    holding the lock is exactly the case the lock exists to protect.
    Release removes the file only while it still holds this acquisition's
    owner record, so a holder that somehow lost the lock cannot delete its
    successor's.

    The default timeout allows for a project-scope snapshot refresh, which
    paginates the whole Project and then queries pull requests per
    repository with retries, legitimately running well past ten seconds.
    """

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self.record: dict[str, Any] | None = None
        self.identity: tuple[int, int] | None = None
        self._checked_owner: tuple[tuple[int, int] | None, int] | None = None
        self._owner_predates = True

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
                if pid is not None and (
                    not pid_running(pid) or not self._verified_owner_predates(identity, pid)
                ):
                    remove_stale_lock(self.path, identity, pid)
                    continue
                if pid is None and self._unreadable_and_stale():
                    remove_stale_lock(self.path, identity, None)
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"timed out waiting for lock {self.path}")
                time.sleep(0.05)
                continue
            try:
                self.record, self.identity = write_owner_record(descriptor)
            except Exception:
                # The created file's identity must come from the descriptor:
                # reading it back from the path would bless whatever file is
                # there now, possibly a successor's live lock.
                created = os.fstat(descriptor)
                os.close(descriptor)
                unlink_if_unchanged(self.path, (created.st_dev, created.st_ino))
                raise
            os.close(descriptor)
            return self

    def __exit__(self, *_args: Any) -> None:
        if self.record is not None:
            release_owner_record(self.path, self.record, self.identity)
            self.record = None
            self.identity = None

    def _verified_owner_predates(self, identity: tuple[int, int] | None, pid: int) -> bool:
        """Cache the recycled-PID verdict per lock file and owner.

        The verdict cannot change while the same file and PID persist (the
        two ages grow in lockstep), and caching keeps the waiter loop from
        spawning a ``ps`` process every polling interval.
        """

        key = (identity, pid)
        if self._checked_owner != key:
            self._checked_owner = key
            self._owner_predates = owner_predates_lock(self.path, pid)
        return self._owner_predates

    def _unreadable_and_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age > UNREADABLE_LOCK_STALE_SECONDS
