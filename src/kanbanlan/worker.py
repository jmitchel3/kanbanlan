from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kanbanlan.accounts import AccountStore, account_env, account_token
from kanbanlan.config import Config, cache_dir
from kanbanlan.github import GitHub
from kanbanlan.locks import file_identity as _file_identity
from kanbanlan.locks import lock_pid as _lock_pid
from kanbanlan.locks import owner_predates_lock as _owner_predates_lock
from kanbanlan.locks import pid_running as _pid_running
from kanbanlan.locks import read_owner_record, release_owner_record, write_owner_record
from kanbanlan.locks import remove_stale_lock as _remove_stale_lock
from kanbanlan.locks import unlink_if_unchanged as _unlink_if_unchanged
from kanbanlan.outbox import drain_outbox
from kanbanlan.registry import (
    Registration,
    RegistryStore,
    group_by_repository,
    last_activity,
    preferred_registration,
    registration_repository_key,
    registry_problems,
    root_state,
    utc_now,
)
from kanbanlan.runner import RateLimitError, Runner
from kanbanlan.snapshot import CacheStore
from kanbanlan.workflow import apply_reconciliation, plan_reconciliation, read_board

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
        self.record: dict[str, Any] | None = None
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
                if pid and _pid_running(pid) and _owner_predates_lock(self.path, pid):
                    raise WorkerAlreadyRunning(f"worker process {pid} already holds {self.path}")
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if pid is None and age < 1:
                    raise WorkerAlreadyRunning(f"worker lock {self.path} is being initialized")
                _remove_stale_lock(self.path, identity, pid)
                continue

            try:
                self.record, self.identity = write_owner_record(descriptor)
            except Exception:
                # The created file's identity must come from the descriptor:
                # reading it back from the path would bless whatever file is
                # there now, possibly a successor's live lock.
                created = os.fstat(descriptor)
                os.close(descriptor)
                _unlink_if_unchanged(self.path, (created.st_dev, created.st_ino))
                raise
            os.close(descriptor)
            break
        self.acquired = True
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.acquired and self.record is not None:
            release_owner_record(self.path, self.record, self.identity)

    def still_held(self) -> bool:
        """Confirm this acquisition still owns the lock, and mark it current.

        Touching the file keeps a long-lived owner's lock young, so even
        where the owner's age cannot be read the lock never outgrows the
        unverifiable-owner cap and gets swept from under a live worker.
        """

        if not self.acquired or self.record is None:
            return False
        if self.identity is not None and _file_identity(self.path) != self.identity:
            return False
        current = read_owner_record(self.path)
        if (
            current is None
            or current.get("pid") != self.record.get("pid")
            or current.get("nonce") != self.record.get("nonce")
        ):
            return False
        try:
            os.utime(self.path)
        except OSError:
            return False
        return True


class GraphQLPointMeter:
    """Delegate to a runner and total the GraphQL points its calls report.

    Every GraphQL document goes through ``run`` as ``gh api graphql``. A
    response that reports ``rateLimit.cost`` adds that cost; one that does
    not (a mutation) adds one point, GitHub's charge for it.
    """

    def __init__(self, runner: Any):
        self._runner = runner
        self._lock = threading.Lock()
        self.points = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runner, name)

    def run(self, args: list[str], *positional: Any, **options: Any) -> Any:
        result = self._runner.run(args, *positional, **options)
        if list(args[:3]) == ["gh", "api", "graphql"]:
            self._add(getattr(result, "stdout", ""))
        return result

    def _add(self, stdout: Any) -> None:
        cost = 1
        try:
            payload = json.loads(stdout)
            reported = ((payload.get("data") or {}).get("rateLimit") or {}).get("cost")
        except (TypeError, ValueError, AttributeError):
            reported = None
        if isinstance(reported, int) and not isinstance(reported, bool) and reported >= 0:
            cost = reported
        with self._lock:
            self.points += cost


def project_key(registration: Registration) -> str | None:
    """Identify the Project a registration refreshes, or None when unreadable."""

    try:
        config = Config.load(Path(registration.root).resolve())
    except Exception:
        return None
    return f"{registration.hostname}/{config.project_owner}/{config.project_number}".lower()


def token_env_name(hostname: str, login: str) -> str:
    digest = hashlib.sha256(f"{hostname}:{login}".encode()).hexdigest()[:16].upper()
    return f"KANBANLAN_GH_TOKEN_{digest}"


def registration_account(registration: Registration) -> str:
    bound = AccountStore().lookup(registration.hostname, registration.repository)
    login = bound.login if bound else registration.github_login
    return f"{registration.hostname}:{login}".lower() if login else ""


def scoped_runner(registration: Registration) -> Runner:
    """Return a runner acting as the account this repository is bound to.

    A user-level binding (``kanbanlan account use``) wins over the login
    recorded at registration, so rebinding takes effect on the next cycle
    without re-registering.
    """

    bound = AccountStore().lookup(registration.hostname, registration.repository)
    login = bound.login if bound else registration.github_login
    if not login:
        raise RuntimeError(
            "repository has no bound GitHub account; run 'kanbanlan account use LOGIN'"
        )
    token_name = token_env_name(registration.hostname, login)
    token = os.environ.get(token_name) or account_token(registration.hostname, login)
    return Runner(Path(registration.root), env=account_env(registration.hostname, token))


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
        registrations = self.registry.registrations()
        # Persisted failure metadata carries the account that actually failed,
        # so rebinding a repository never transfers its cooldown to a new login.
        cooldowns: dict[str, datetime] = {}
        for registration in registrations:
            failure = registration.last_error or {}
            account = failure.get("rate_limit_account")
            retry_at = _parse_time(registration.next_retry_at)
            if account and failure.get("kind") == "RateLimitError" and retry_at and retry_at > now:
                cooldowns[account] = max(cooldowns.get(account, now), retry_at)

        enabled: list[Registration] = []
        for registration in registrations:
            if not registration.enabled or registration.disabled:
                summary["skipped"] += 1
            else:
                enabled.append(registration)

        # One repository registered from several clones (a live checkout and a
        # forgotten copy) would otherwise refresh the same board once per
        # clone. Only the preferred clone is serviced; status reports the rest.
        due: list[Registration] = []
        for group in group_by_repository(enabled).values():
            chosen = preferred_registration(group) if len(group) > 1 else group[0]
            for registration in group:
                if registration is not chosen:
                    summary["skipped"] += 1
                    summary["repositories"].append(
                        {
                            "repository": registration.repository,
                            "root": registration.root,
                            "status": "duplicate",
                            "serviced_root": chosen.root,
                        }
                    )
            retry_at = _parse_time(chosen.next_retry_at)
            if retry_at and retry_at > now:
                summary["skipped"] += 1
                continue
            last_run = _parse_time(chosen.last_run_at)
            if last_run and last_run + timedelta(seconds=chosen.interval_seconds) > now:
                summary["skipped"] += 1
                continue
            due.append(chosen)

        # Repository snapshots are repository-scoped: each one paginates the
        # whole Project but keeps only its own repository's content, so one
        # repository's refresh cannot stand in for another's. Instead, a
        # Project is refreshed at most once per cycle, rotating through the
        # repositories that share it by oldest last run, so a shared Project
        # costs one refresh per interval rather than one per repository.
        due.sort(key=lambda value: value.last_run_at or "")
        refreshed_projects: dict[str, str] = {}
        for registration in due:
            project = project_key(registration)
            if project is not None and project in refreshed_projects:
                summary["skipped"] += 1
                summary["repositories"].append(
                    {
                        "repository": registration.repository,
                        "status": "project_refreshed",
                        "refreshed_by": refreshed_projects[project],
                    }
                )
                continue
            summary["attempted"] += 1
            account = ""
            try:
                account = registration_account(registration)
                if cooldowns.get(account, now) > now:
                    summary["attempted"] -= 1
                    summary["skipped"] += 1
                    continue
                self._run_registration(registration)
            except Exception as exc:  # worker must continue servicing other repositories
                if isinstance(exc, RateLimitError) and account:
                    retry_at = _parse_time(registration.next_retry_at)
                    if retry_at:
                        cooldowns[account] = retry_at
                summary["failed"] += 1
                summary["repositories"].append(
                    {
                        "repository": registration.repository,
                        "status": "error",
                        "error": str(exc),
                    }
                )
            else:
                if project is not None:
                    refreshed_projects[project] = registration.repository
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
        meter: GraphQLPointMeter | None = None
        try:
            root = Path(registration.root).resolve()
            config = Config.load(root)
            meter = GraphQLPointMeter(scoped_runner(registration))
            provider = GitHub(root, config, runner=meter)
            store = CacheStore(config, cache_dir(root))
            store.check_refresh_allowed()
            # Changes a session queued are normally drained at once by a
            # process that session started; this catches any that process
            # never finished. Another drainer already running wins.
            drain_outbox(root, store, provider)
            snapshot, open_issues = read_board(store, provider)
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
                verified, verified_issues = read_board(store, provider)
                verification_drift = plan_reconciliation(verified, verified_issues)
                if verification_drift:
                    raise RuntimeError(
                        "verification found unresolved differences: "
                        + "; ".join(value.kind for value in verification_drift)
                    )
            registration.last_success_at = utc_now()
            registration.last_graphql_points = meter.points
            registration.consecutive_failures = 0
            registration.next_retry_at = None
            registration.last_error = None
        except Exception as exc:
            registration.last_graphql_points = meter.points if meter else 0
            registration.consecutive_failures += 1
            delay = min(
                MAX_BACKOFF_SECONDS,
                30 * (2 ** max(0, registration.consecutive_failures - 1)),
            )
            retry_at = datetime.now(UTC) + timedelta(seconds=delay)
            registration.last_error = {"kind": exc.__class__.__name__, "message": str(exc)}
            if isinstance(exc, RateLimitError):
                # A quota reset supersedes ordinary per-repository backoff.
                # Without one, wait at least a normal polling interval.
                try:
                    reset_at = _parse_time(exc.reset_at)
                    if reset_at and reset_at > datetime.now(UTC):
                        retry_at = reset_at
                    else:
                        retry_at = max(
                            retry_at,
                            datetime.now(UTC) + timedelta(seconds=registration.interval_seconds),
                        )
                except (ValueError, TypeError):
                    retry_at = max(
                        retry_at,
                        datetime.now(UTC) + timedelta(seconds=registration.interval_seconds),
                    )
                registration.last_error["rate_limit_account"] = registration_account(registration)
            registration.next_retry_at = retry_at.isoformat().replace("+00:00", "Z")
            self.registry.update(registration)
            raise
        self.registry.update(registration)

    def run_forever(self, *, once: bool = False) -> dict[str, Any] | None:
        if once:
            return self.run_once()
        lock = WorkerLock(self.lock_path).__enter__()
        try:
            while True:
                self._run_all()
                enabled_intervals = [
                    value.interval_seconds
                    for value in self.registry.registrations()
                    if value.enabled and not value.disabled
                ]
                self.sleep(min([self.interval_seconds, *enabled_intervals]))
                if lock.still_held():
                    continue
                # The lock was taken over or swept. Reclaim it when it is
                # free; when another live worker holds it, exit rather than
                # run a second loop beside it.
                lock.__exit__(None, None, None)
                try:
                    lock = WorkerLock(self.lock_path).__enter__()
                except WorkerAlreadyRunning as exc:
                    return {"stopped": True, "reason": str(exc)}
        finally:
            lock.__exit__(None, None, None)


def worker_status(registry: RegistryStore | None = None) -> dict[str, Any]:
    registry = registry or RegistryStore()
    lock_path = registry.directory / "worker.lock"
    identity = _file_identity(lock_path)
    pid = _lock_pid(lock_path)
    running = bool(pid and _pid_running(pid) and _owner_predates_lock(lock_path, pid))
    if identity and not running:
        try:
            old_enough_to_be_stale = time.time() - lock_path.stat().st_mtime >= 1
        except FileNotFoundError:
            old_enough_to_be_stale = False
        if pid is not None or old_enough_to_be_stale:
            _remove_stale_lock(lock_path, identity, pid)
        pid = None
    registrations = registry.registrations()
    serviced = {
        key: preferred_registration(group).common_dir
        for key, group in group_by_repository(
            [value for value in registrations if value.enabled and not value.disabled]
        ).items()
    }
    repositories = []
    for registration in registrations:
        value = asdict_registration(registration)
        chosen = serviced.get(registration_repository_key(registration))
        value["duplicate_skipped"] = chosen is not None and chosen != registration.common_dir
        repositories.append(value)
    return {
        "state_dir": str(registry.directory),
        "worker": {"pid": pid, "running": running},
        "repositories": repositories,
        "problems": registry_problems(registrations),
    }


def asdict_registration(registration: Registration) -> dict[str, Any]:
    activity = last_activity(registration)
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
        "last_graphql_points": registration.last_graphql_points,
        "root_state": root_state(registration),
        "last_activity_at": (
            datetime.fromtimestamp(activity, UTC).isoformat().replace("+00:00", "Z")
            if activity is not None
            else None
        ),
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
