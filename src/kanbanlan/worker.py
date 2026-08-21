from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kanbanlan.config import Config, cache_dir
from kanbanlan.github import GitHub
from kanbanlan.registry import Registration, RegistryStore, utc_now
from kanbanlan.runner import Runner
from kanbanlan.snapshot import CacheStore
from kanbanlan.workflow import apply_reconciliation, plan_reconciliation

MAX_BACKOFF_SECONDS = 3600
DEFAULT_INTERVAL_SECONDS = 300


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class WorkerAlreadyRunning(RuntimeError):
    pass


class WorkerLock:
    """Atomic process lock that removes state only when its recorded owner is gone."""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False
        self.identity: tuple[int, int] | None = None

    def __enter__(self) -> WorkerLock:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                identity = _file_identity(self.path)
                pid = _lock_pid(self.path)
                if pid and _pid_running(pid):
                    raise WorkerAlreadyRunning(f"worker process {pid} already holds {self.path}")
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if pid is None and age < 1:
                    raise WorkerAlreadyRunning(f"worker lock {self.path} is being initialized")
                _unlink_if_unchanged(self.path, identity)
                continue

            try:
                payload = (
                    json.dumps({"pid": os.getpid(), "started_at": utc_now()}) + "\n"
                ).encode()
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("could not write the complete worker lock")
                os.fsync(descriptor)
                stat = os.fstat(descriptor)
                self.identity = (stat.st_dev, stat.st_ino)
            except Exception:
                os.close(descriptor)
                _unlink_if_unchanged(self.path, _file_identity(self.path))
                raise
            os.close(descriptor)
            break
        self.acquired = True
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.acquired:
            _unlink_if_unchanged(self.path, self.identity)


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def _unlink_if_unchanged(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None or _file_identity(path) != identity:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _lock_pid(path: Path) -> int | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pid = int(value.get("pid", 0))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    return pid if pid > 0 else None


def _pid_running(pid: int) -> bool:
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


def token_env_name(hostname: str, login: str) -> str:
    digest = hashlib.sha256(f"{hostname}:{login}".encode()).hexdigest()[:16].upper()
    return f"KANBANLAN_GH_TOKEN_{digest}"


def scoped_runner(registration: Registration) -> Runner:
    if not registration.github_login:
        raise RuntimeError(
            "repository has no recorded GitHub account; run worker enable --github-login"
        )
    token_name = token_env_name(registration.hostname, registration.github_login)
    token = os.environ.get(token_name)
    if token is None:
        token_result = Runner(
            Path(registration.root),
            env={
                "GH_HOST": registration.hostname,
                "GH_TOKEN": None,
                "GITHUB_TOKEN": None,
                "GH_ENTERPRISE_TOKEN": None,
            },
        ).run(
            [
                "gh",
                "auth",
                "token",
                "--hostname",
                registration.hostname,
                "--user",
                registration.github_login,
            ]
        )
        token = token_result.stdout.strip()
    if not token:
        raise RuntimeError(f"GitHub account {registration.github_login!r} has no usable token")
    return Runner(
        Path(registration.root),
        env={"GH_HOST": registration.hostname, "GH_TOKEN": token},
    )


class Worker:
    def __init__(
        self,
        registry: RegistryStore | None = None,
        *,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        sleep=time.sleep,
    ):
        self.registry = registry or RegistryStore()
        self.interval_seconds = max(30, int(interval_seconds))
        self.sleep = sleep

    @property
    def lock_path(self) -> Path:
        return self.registry.directory / "worker.lock"

    def run_once(self) -> dict[str, Any]:
        with WorkerLock(self.lock_path):
            return self._run_all()

    def _run_all(self) -> dict[str, Any]:
        summary = {"attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0, "repositories": []}
        now = datetime.now(UTC)
        for registration in self.registry.registrations():
            if not registration.enabled or registration.disabled:
                summary["skipped"] += 1
                continue
            retry_at = _parse_time(registration.next_retry_at)
            if retry_at and retry_at > now:
                summary["skipped"] += 1
                continue
            last_run = _parse_time(registration.last_run_at)
            if last_run and last_run + timedelta(seconds=registration.interval_seconds) > now:
                summary["skipped"] += 1
                continue
            summary["attempted"] += 1
            try:
                self._run_registration(registration)
            except Exception as exc:  # worker must continue servicing other repositories
                summary["failed"] += 1
                summary["repositories"].append(
                    {
                        "repository": registration.repository,
                        "status": "error",
                        "error": str(exc),
                    }
                )
            else:
                summary["succeeded"] += 1
                summary["repositories"].append(
                    {"repository": registration.repository, "status": "ok"}
                )
        return summary

    def _run_registration(self, registration: Registration) -> None:
        now = utc_now()
        registration.last_run_at = now
        registration.last_error = None
        self.registry.update(registration)
        try:
            root = Path(registration.root).resolve()
            config = Config.load(root)
            runner = scoped_runner(registration)
            provider = GitHub(root, config, runner=runner)
            store = CacheStore(config, cache_dir(root))
            snapshot = store.refresh(provider)
            open_issues = provider.list_open_requests()
            drift = plan_reconciliation(snapshot, open_issues)
            unsafe = [value for value in drift if value.kind == "duplicate_kanbanlan_id"]
            if unsafe:
                raise RuntimeError("unresolved duplicate Kanbanlan identities; safe repair skipped")
            if drift:
                remaining, _ = apply_reconciliation(provider, store, snapshot, open_issues)
                if remaining:
                    raise RuntimeError(
                        "reconciliation left unresolved differences: "
                        + "; ".join(value.kind for value in remaining)
                    )
                # Verification re-reads live state only after something was
                # repaired. A clean cycle already proved itself with the read
                # above, and the GraphQL point budget it would spend here is
                # shared by every repository and agent on this account.
                verified = store.refresh(provider)
                verification_drift = plan_reconciliation(verified, provider.list_open_requests())
                if verification_drift:
                    raise RuntimeError(
                        "verification found unresolved differences: "
                        + "; ".join(value.kind for value in verification_drift)
                    )
            registration.last_success_at = utc_now()
            registration.consecutive_failures = 0
            registration.next_retry_at = None
            registration.last_error = None
        except Exception as exc:
            registration.consecutive_failures += 1
            delay = min(
                MAX_BACKOFF_SECONDS,
                30 * (2 ** max(0, registration.consecutive_failures - 1)),
            )
            registration.next_retry_at = (
                (datetime.now(UTC) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
            )
            registration.last_error = {"kind": exc.__class__.__name__, "message": str(exc)}
            self.registry.update(registration)
            raise
        self.registry.update(registration)

    def run_forever(self, *, once: bool = False) -> dict[str, Any] | None:
        if once:
            return self.run_once()
        with WorkerLock(self.lock_path):
            while True:
                self._run_all()
                enabled_intervals = [
                    value.interval_seconds
                    for value in self.registry.registrations()
                    if value.enabled and not value.disabled
                ]
                self.sleep(min([self.interval_seconds, *enabled_intervals]))


def worker_status(registry: RegistryStore | None = None) -> dict[str, Any]:
    registry = registry or RegistryStore()
    lock_path = registry.directory / "worker.lock"
    identity = _file_identity(lock_path)
    pid = _lock_pid(lock_path)
    running = bool(pid and _pid_running(pid))
    if identity and not running:
        try:
            old_enough_to_be_stale = time.time() - lock_path.stat().st_mtime >= 1
        except FileNotFoundError:
            old_enough_to_be_stale = False
        if pid is not None or old_enough_to_be_stale:
            _unlink_if_unchanged(lock_path, identity)
        pid = None
    return {
        "state_dir": str(registry.directory),
        "worker": {"pid": pid, "running": running},
        "repositories": [asdict_registration(value) for value in registry.registrations()],
    }


def asdict_registration(registration: Registration) -> dict[str, Any]:
    return {
        "common_dir": registration.common_dir,
        "root": registration.root,
        "repository": registration.repository,
        "hostname": registration.hostname,
        "github_login": registration.github_login,
        "enabled": registration.enabled,
        "disabled": registration.disabled,
        "registered_at": registration.registered_at,
        "last_run_at": registration.last_run_at,
        "last_success_at": registration.last_success_at,
        "last_error": registration.last_error,
        "consecutive_failures": registration.consecutive_failures,
        "next_retry_at": registration.next_retry_at,
        "interval_seconds": registration.interval_seconds,
    }


def start_worker(
    registry: RegistryStore | None = None, *, interval_seconds: int = 300
) -> dict[str, Any]:
    registry = registry or RegistryStore()
    status = worker_status(registry)
    if status["worker"]["running"]:
        return status
    registry.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(registry.directory, 0o700)
    except OSError:
        pass
    log_path = registry.directory / "worker.log"
    log = log_path.open("a", encoding="utf-8")
    env = os.environ.copy()
    process = subprocess.Popen(
        [sys.executable, "-m", "kanbanlan", "worker", "run", "--interval", str(interval_seconds)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        env=env,
    )
    log.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = worker_status(registry)
        if status["worker"]["running"]:
            if status["worker"]["pid"] != process.pid and process.poll() is None:
                process.terminate()
            return status
        returncode = process.poll()
        if returncode is not None:
            detail = ""
            try:
                detail = log_path.read_text(encoding="utf-8")[-2000:].strip()
            except OSError:
                pass
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"worker exited during startup with status {returncode}{suffix}")
        time.sleep(0.05)
    process.terminate()
    raise RuntimeError("worker did not acquire its process lock during startup")


def stop_worker(registry: RegistryStore | None = None) -> dict[str, Any]:
    registry = registry or RegistryStore()
    status = worker_status(registry)
    pid = status["worker"].get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while _pid_running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_running(pid):
            raise RuntimeError(f"worker process {pid} did not stop after SIGTERM")
    return worker_status(registry)
