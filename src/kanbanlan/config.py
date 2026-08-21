from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kanbanlan.runner import Runner

CONFIG_FILENAME = ".kanbanlan.toml"
CONFIG_SCHEMA_VERSION = 2
REMOTE_RE = re.compile(
    r"(?:git@[^:]+:|https?://[^/]+/|ssh://git@[^/]+/)(?P<repo>[^/]+/[^/]+?)(?:\.git)?$"
)
REPOSITORY_TARGET_RE = re.compile(r"^(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)$")
REPOSITORY_URL_RE = re.compile(
    r"^(?:https?://|ssh://git@|git@)(?P<host>[^/:]+)[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class Config:
    repository: str
    project_owner: str
    project_owner_type: str
    project_number: int
    default_branch: str = "main"
    stage_branch: str = "main"
    production_branch: str = ""
    hostname: str = "github.com"
    stale_seconds: int = 180
    rate_limit_floor: int = 500
    code_host: str = "github"
    canonical_home: str = "github"
    projections: tuple[str, ...] = ("github_projects",)
    session_tracking: bool = False

    def __post_init__(self) -> None:
        if self.repository.count("/") != 1:
            raise ValueError("repository must use owner/name format")
        if self.project_owner_type not in {"organization", "user"}:
            raise ValueError("project_owner_type must be organization or user")
        if self.project_number < 1:
            raise ValueError("project_number must be positive")
        if self.stale_seconds < 1:
            raise ValueError("stale_seconds must be positive")
        if self.rate_limit_floor < 0:
            raise ValueError("rate_limit_floor must be zero or positive")
        if self.code_host != "github":
            raise ValueError(f"unsupported code host: {self.code_host}")
        if self.canonical_home != "github":
            raise ValueError(f"unsupported canonical kanban home: {self.canonical_home}")
        if not self.projections or any(not value for value in self.projections):
            raise ValueError("at least one valid kanban projection is required")
        if not isinstance(self.session_tracking, bool):
            raise ValueError("session_tracking must be true or false")

    @classmethod
    def load(cls, root: Path) -> Config:
        path = root / CONFIG_FILENAME
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"{path} is missing; run 'kanbanlan init' from the repository first"
            ) from exc
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(f"could not read {path}: {exc}") from exc
        try:
            schema_version = int(data.get("schema_version", 1))
            if schema_version < 1 or schema_version > CONFIG_SCHEMA_VERSION:
                raise ValueError(f"unsupported schema_version {schema_version}")
            project = data.get("project", {})
            repository = data.get("repository", {})
            local = data.get("local", {})
            coordination = data.get("coordination", {})
            session_tracking = data.get("session_tracking", {})
            if not isinstance(session_tracking, dict):
                raise ValueError("session_tracking must be a table")
            tracking_enabled = session_tracking.get("enabled", False)
            if not isinstance(tracking_enabled, bool):
                raise ValueError("session_tracking.enabled must be true or false")
            projections = coordination.get("projections", ["github_projects"])
            if not isinstance(projections, list) or not all(
                isinstance(value, str) for value in projections
            ):
                raise ValueError("coordination.projections must be a list of strings")
            return cls(
                repository=repository["name_with_owner"],
                project_owner=project["owner"],
                project_owner_type=project.get("owner_type", "organization"),
                project_number=int(project["number"]),
                default_branch=repository.get("default_branch", "main"),
                stage_branch=repository.get(
                    "stage_branch", repository.get("default_branch", "main")
                ),
                production_branch=repository.get("production_branch", ""),
                hostname=repository.get("hostname", "github.com"),
                stale_seconds=int(local.get("stale_seconds", 180)),
                rate_limit_floor=int(local.get("rate_limit_floor", 500)),
                code_host=coordination.get("code_host", "github"),
                canonical_home=coordination.get("canonical_home", "github"),
                projections=tuple(projections),
                session_tracking=tracking_enabled,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{path} is incomplete or invalid ({exc}); "
                "rerun 'kanbanlan init --reconfigure' to repair it"
            ) from exc

    def to_toml(self) -> str:
        projections = ", ".join(f'"{_escape(value)}"' for value in self.projections)
        return (
            f"schema_version = {CONFIG_SCHEMA_VERSION}\n\n"
            "[coordination]\n"
            f'code_host = "{_escape(self.code_host)}"\n'
            f'canonical_home = "{_escape(self.canonical_home)}"\n'
            f"projections = [{projections}]\n\n"
            "[repository]\n"
            f'name_with_owner = "{_escape(self.repository)}"\n'
            f'hostname = "{_escape(self.hostname)}"\n'
            f'default_branch = "{_escape(self.default_branch)}"\n'
            f'stage_branch = "{_escape(self.stage_branch)}"\n'
            f'production_branch = "{_escape(self.production_branch)}"\n\n'
            "[project]\n"
            f'owner = "{_escape(self.project_owner)}"\n'
            f'owner_type = "{_escape(self.project_owner_type)}"\n'
            f"number = {self.project_number}\n\n"
            "[local]\n"
            f"stale_seconds = {self.stale_seconds}\n"
            f"rate_limit_floor = {self.rate_limit_floor}\n\n"
            "[session_tracking]\n"
            f"enabled = {'true' if self.session_tracking else 'false'}\n"
        )

    def session_tracking_enabled(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> bool:
        values = environ if environ is not None else os.environ
        override = values.get("KANBANLAN_SESSION_TRACKING")
        if override is None:
            return self.session_tracking
        normalized = override.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError("KANBANLAN_SESSION_TRACKING must be true/false, 1/0, yes/no, or on/off")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def find_repo_root(start: Path | None = None) -> Path:
    runner = Runner((start or Path.cwd()).resolve())
    result = runner.run(["git", "rev-parse", "--show-toplevel"])
    return Path(result.stdout.strip()).resolve()


def discover_repository(root: Path) -> str:
    remote = Runner(root).run(["git", "remote", "get-url", "origin"]).stdout.strip()
    match = REMOTE_RE.search(remote)
    if not match:
        raise RuntimeError(f"could not derive owner/name from origin URL: {remote}")
    return match.group("repo")


def discover_default_branch(root: Path) -> str:
    runner = Runner(root)
    symbolic = runner.run(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if symbolic.returncode == 0 and "/" in symbolic.stdout:
        return symbolic.stdout.strip().split("/", 1)[1]
    return "main"


def normalize_repository_target(value: str, *, hostname: str) -> str:
    """Return ``OWNER/REPO`` for an explicitly named repository.

    A bare ``OWNER/REPO`` is accepted as-is. A URL is accepted only when its
    host matches the configured one, because a request on another GitHub host
    is not addressable by the same authenticated client.
    """

    text = value.strip().removesuffix("/")
    if not text:
        raise RuntimeError("repository target must not be empty")
    match = REPOSITORY_TARGET_RE.match(text)
    if match:
        return match.group("repo")
    url = REPOSITORY_URL_RE.match(text)
    if not url:
        raise RuntimeError(f"repository target must use OWNER/REPO format: {value!r}")
    if url.group("host").casefold() != hostname.casefold():
        raise RuntimeError(
            f"repository target {value!r} is on {url.group('host')}, "
            f"but this repository is configured for {hostname}"
        )
    return url.group("repo")


def cache_dir(root: Path) -> Path:
    return common_dir(root).parent / ".cache" / "kanbanlan"


def common_dir(root: Path) -> Path:
    """Return the resolved Git common directory shared by all worktrees."""
    result = Runner(root).run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    return Path(result.stdout.strip()).resolve()


def primary_worktree(root: Path) -> Path:
    """Return the stable primary checkout instead of a disposable linked worktree."""
    result = Runner(root).run(["git", "worktree", "list", "--porcelain"])
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree ")).resolve()
    raise RuntimeError("git did not report a primary worktree")
