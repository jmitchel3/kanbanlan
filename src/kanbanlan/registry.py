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

    def __post_init__(self) -> None:
        if not self.registered_at:
            self.registered_at = utc_now()
        self.enabled = bool(self.enabled and not self.disabled)
        self.interval_seconds = max(30, int(self.interval_seconds or 300))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Registration:
        fields = {field: value[field] for field in cls.__dataclass_fields__ if field in value}
        return cls(**fields)


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
