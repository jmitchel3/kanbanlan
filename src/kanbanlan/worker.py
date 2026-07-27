from __future__ import annotations

import hashlib
import json
import os
import signal
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
    """Long-lived process lock that checks PID liveness before removing stale state."""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self) -> WorkerLock:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                pid = int(value.get("pid", 0))
            except (OSError, ValueError, json.JSONDecodeError):
                pid = 0
            if pid and _pid_running(pid):
                raise WorkerAlreadyRunning(f"worker process {pid} already holds {self.path}")
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": utc_now()}) + "\n",
            encoding="utf-8",
        )
        os.chmod(self.path, 0o600)
        self.acquired = True
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.acquired:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if int(value.get("pid", 0)) == os.getpid():
                    self.path.unlink()
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                pass


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
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
        token_result = Runner(Path(registration.root)).run(
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
        summary = {"attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0, "repositories": []}
        with WorkerLock(self.lock_path):
            for registration in self.registry.registrations():
                if not registration.enabled or registration.disabled:
                    summary["skipped"] += 1
                    continue
                retry_at = _parse_time(registration.next_retry_at)
                if retry_at and retry_at > datetime.now(UTC):
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
            verified = store.refresh(provider)
            verification_drift = plan_reconciliation(
                verified, provider.list_open_requests()
            )
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
        while True:
            self.run_once()
            self.sleep(self.interval_seconds)


def worker_status(registry: RegistryStore | None = None) -> dict[str, Any]:
    registry = registry or RegistryStore()
    pid_path = registry.directory / "worker.pid"
    pid: int | None = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
    running = bool(pid and _pid_running(pid))
    if pid and not running:
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
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
    log_path = registry.directory / "worker.log"
    log = log_path.open("a", encoding="utf-8")
    env = os.environ.copy()
    process = __import__("subprocess").Popen(
        [sys.executable, "-m", "kanbanlan", "worker", "run", "--interval", str(interval_seconds)],
        stdin=__import__("subprocess").DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        env=env,
    )
    registry.directory.joinpath("worker.pid").write_text(str(process.pid) + "\n", encoding="utf-8")
    os.chmod(registry.directory / "worker.pid", 0o600)
    log.close()
    return worker_status(registry)


def stop_worker(registry: RegistryStore | None = None) -> dict[str, Any]:
    registry = registry or RegistryStore()
    status = worker_status(registry)
    pid = status["worker"].get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return worker_status(registry)
