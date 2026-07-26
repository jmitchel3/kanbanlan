from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from kanbanlan.runner import Runner

CONFIG_FILENAME = ".kanbanlan.toml"
REMOTE_RE = re.compile(
    r"(?:git@[^:]+:|https?://[^/]+/|ssh://git@[^/]+/)(?P<repo>[^/]+/[^/]+?)(?:\.git)?$"
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

    def __post_init__(self) -> None:
        if self.repository.count("/") != 1:
            raise ValueError("repository must use owner/name format")
        if self.project_owner_type not in {"organization", "user"}:
            raise ValueError("project_owner_type must be organization or user")
        if self.project_number < 1:
            raise ValueError("project_number must be positive")
        if self.stale_seconds < 1:
            raise ValueError("stale_seconds must be positive")

    @classmethod
    def load(cls, root: Path) -> Config:
        path = root / CONFIG_FILENAME
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"{path} is missing; run 'kanbanlan init' from the repository first"
            ) from exc
        project = data.get("project", {})
        repository = data.get("repository", {})
        local = data.get("local", {})
        return cls(
            repository=repository["name_with_owner"],
            project_owner=project["owner"],
            project_owner_type=project.get("owner_type", "organization"),
            project_number=int(project["number"]),
            default_branch=repository.get("default_branch", "main"),
            stage_branch=repository.get("stage_branch", repository.get("default_branch", "main")),
            production_branch=repository.get("production_branch", ""),
            hostname=repository.get("hostname", "github.com"),
            stale_seconds=int(local.get("stale_seconds", 180)),
        )

    def to_toml(self) -> str:
        return (
            "schema_version = 1\n\n"
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
        )


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


def cache_dir(root: Path) -> Path:
    result = Runner(root).run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    common_dir = Path(result.stdout.strip()).resolve()
    return common_dir.parent / ".cache" / "kanbanlan"
