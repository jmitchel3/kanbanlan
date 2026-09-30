from __future__ import annotations

import json
import os
import platform
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kanbanlan.locks import FileLock

REGISTRY_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def state_dir() -> Path:
    override = os.environ.get("KANBANLAN_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Kanbanlan"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kanbanlan"


@dataclass
class Registration:
    common_dir: str
    root: str
    repository: str
    hostname: str
    github_login: str | None
    enabled: bool = True
    disabled: bool = False
    registered_at: str = ""
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_error: dict[str, str] | None = None
    consecutive_failures: int = 0
    next_retry_at: str | None = None
    interval_seconds: int = 300
    # GraphQL points the last worker run reported spending (None before one).
    last_graphql_points: int | None = None

    def __post_init__(self) -> None:
        if not self.registered_at:
            self.registered_at = utc_now()
        self.enabled = bool(self.enabled and not self.disabled)
        self.interval_seconds = max(30, int(self.interval_seconds or 300))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Registration:
        fields = {field: value[field] for field in cls.__dataclass_fields__ if field in value}
        return cls(**fields)


def registration_repository_key(registration: Registration) -> str:
    """Identify the repository a registration serves, independent of its clone."""

    return f"{registration.hostname}/{registration.repository}".lower()


def root_state(registration: Registration) -> str:
    """Report whether a registration's root is still a usable Git checkout."""

    root = Path(registration.root)
    if not root.is_dir():
        return "missing_root"
    if not (root / ".git").exists():
        return "not_a_git_checkout"
    return "ok"


# Files Git rewrites on checkout, commit, fetch, and staging, in the common
# directory and in each linked worktree's private directory.
_ACTIVITY_FILES = ("HEAD", "index", "FETCH_HEAD", "logs/HEAD")


def last_activity(registration: Registration) -> float | None:
    """Return the newest Git activity time for a registration's clone.

    A clone someone works in touches these files constantly; a forgotten
    clone does not, which is what tells the live checkout from a stale copy
    when one repository is registered twice.
    """

    common = Path(registration.common_dir)
    candidates = [common / name for name in _ACTIVITY_FILES]
    try:
        for worktree in (common / "worktrees").iterdir():
            candidates.extend(worktree / name for name in _ACTIVITY_FILES)
    except OSError:
        pass
    times: list[float] = []
    for candidate in candidates:
        try:
            times.append(candidate.stat().st_mtime)
        except OSError:
            continue
    return max(times) if times else None


def preferred_registration(group: list[Registration]) -> Registration:
    """Pick the one registration of a repository the worker should service.

    A usable checkout beats a missing or non-Git root, then the most recently
    active clone wins, then the most recently successful one.
    """

    return max(
        group,
        key=lambda value: (
            root_state(value) == "ok",
            last_activity(value) or 0.0,
            value.last_success_at or "",
            value.registered_at,
        ),
    )


def group_by_repository(registrations: list[Registration]) -> dict[str, list[Registration]]:
    groups: dict[str, list[Registration]] = {}
    for registration in registrations:
        groups.setdefault(registration_repository_key(registration), []).append(registration)
    return groups


def registry_problems(registrations: list[Registration]) -> list[dict[str, Any]]:
    """Describe registrations that waste refreshes or can no longer run."""

    problems: list[dict[str, Any]] = []
    for group in group_by_repository(registrations).values():
        if len(group) < 2:
            continue
        active = [value for value in group if value.enabled and not value.disabled]
        chosen = preferred_registration(active) if active else None
        problems.append(
            {
                "kind": "duplicate_repository",
                "repository": group[0].repository,
                "roots": sorted(value.root for value in group),
                "serviced_root": chosen.root if chosen else None,
            }
        )
    for registration in registrations:
        state = root_state(registration)
        if state != "ok":
            problems.append(
                {
                    "kind": state,
                    "repository": registration.repository,
                    "root": registration.root,
                }
            )
    return problems


def describe_problem(problem: dict[str, Any]) -> str:
    kind = problem["kind"]
    if kind == "duplicate_repository":
        others = [root for root in problem["roots"] if root != problem.get("serviced_root")]
        serviced = problem.get("serviced_root") or "none (all disabled)"
        return (
            f"{problem['repository']} is registered {len(problem['roots'])} times; "
            f"the worker services {serviced} and skips {', '.join(others)}"
        )
    if kind == "missing_root":
        return f"{problem['repository']} is registered at {problem['root']}, which no longer exists"
    return (
        f"{problem['repository']} is registered at {problem['root']}, which is not a Git checkout"
    )


class RegistryStore:
    """Atomic user-scoped repository registry, keyed by Git common directory."""

    def __init__(self, directory: Path | None = None):
        self.directory = (directory or state_dir()).resolve()
        self.path = self.directory / "registry.json"
        self.lock_path = self.directory / "registry.lock"

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            value = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read worker registry {self.path}: {exc}") from exc
        repositories = value.get("repositories", {}) if isinstance(value, dict) else {}
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "repositories": repositories if isinstance(repositories, dict) else {},
        }

    def registrations(self) -> list[Registration]:
        return [Registration.from_dict(value) for value in self.load()["repositories"].values()]

    def get(self, common_dir: str) -> Registration | None:
        value = self.load()["repositories"].get(str(Path(common_dir).resolve()))
        return Registration.from_dict(value) if value else None

    def register(
        self,
        *,
        common_dir: Path,
        root: Path,
        repository: str,
        hostname: str,
        github_login: str | None,
        interval_seconds: int = 300,
    ) -> Registration:
        key = str(common_dir.resolve())
        self._prepare_directory()
        with FileLock(self.lock_path):
            data = self.load()
            existing = data["repositories"].get(key)
            if existing:
                registration = Registration.from_dict(existing)
                existing_root = Path(registration.root)
                if not existing_root.exists():
                    registration.root = str(root.resolve())
                registration.repository = repository
                registration.hostname = hostname
                registration.github_login = github_login or registration.github_login
                registration.interval_seconds = max(30, int(interval_seconds))
            else:
                registration = Registration(
                    common_dir=key,
                    root=str(root.resolve()),
                    repository=repository,
                    hostname=hostname,
                    github_login=github_login,
                    interval_seconds=interval_seconds,
                )
            data["repositories"][key] = asdict(registration)
            self._save(data)
            return registration

    def enable(self, common_dir: Path) -> Registration:
        return self._set_enabled(common_dir, True)

    def disable(self, common_dir: Path) -> Registration:
        return self._set_enabled(common_dir, False)

    def update(self, registration: Registration) -> None:
        key = str(Path(registration.common_dir).resolve())
        self._prepare_directory()
        with FileLock(self.lock_path):
            data = self.load()
            data["repositories"][key] = asdict(registration)
            self._save(data)

    def _set_enabled(self, common_dir: Path, enabled: bool) -> Registration:
        key = str(common_dir.resolve())
        self._prepare_directory()
        with FileLock(self.lock_path):
            data = self.load()
            value = data["repositories"].get(key)
            if not value:
                raise RuntimeError(f"repository {key} is not registered")
            registration = Registration.from_dict(value)
            registration.disabled = not enabled
            registration.enabled = enabled
            data["repositories"][key] = asdict(registration)
            self._save(data)
            return registration

    def _save(self, data: dict[str, Any]) -> None:
        self._prepare_directory()
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory, prefix=".registry.", delete=False
            ) as stream:
                temporary = stream.name
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _prepare_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
