from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from kanbanlan.config import Config


@dataclass(frozen=True)
class ProviderCapabilities:
    comments: bool = True
    claims: bool = True
    priorities: bool = True
    pull_request_links: bool = True
    projections: bool = True
    repository_records: bool = True
    project_scope: bool = True
    repository_routing: bool = True
    request_rehoming: bool = True


class CoordinationProvider(Protocol):
    provider_name: str
    capabilities: ProviderCapabilities

    def snapshot(self, *, generated_at: datetime) -> dict[str, Any]: ...

    def list_open_requests(self) -> list[dict[str, Any]]: ...

    def add_to_projection(self, url: str) -> dict[str, Any]: ...

    def set_request_status(
        self,
        reference: int | str,
        label: str | None,
        *,
        repository: str | None = None,
    ) -> None: ...

    def set_projection_status(
        self,
        item_id: str,
        projection: dict[str, Any],
        status: str,
    ) -> None: ...

    def comment_request(
        self,
        reference: int | str,
        body: str,
        *,
        repository: str | None = None,
    ) -> None: ...

    def create_request(
        self,
        title: str,
        body: str,
        priority: str,
        *,
        repository: str | None = None,
    ) -> str: ...

    def ensure_request_identity(
        self,
        reference: int | str,
        kanbanlan_id: str,
        *,
        repository: str | None = None,
    ) -> str: ...

    def inspect_repository_target(self, repository: str) -> dict[str, Any]: ...

    def prepare_repository_target(self, repository: str) -> dict[str, Any]: ...

    def transfer_request(
        self,
        reference: int | str,
        target: str,
        *,
        repository: str | None = None,
    ) -> str: ...


def create_provider(root: Path, config: Config) -> CoordinationProvider:
    if config.canonical_home == "github":
        from kanbanlan.github import GitHub

        return GitHub(root, config)
    raise RuntimeError(f"canonical kanban home {config.canonical_home!r} is not supported")
