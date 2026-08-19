from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kanbanlan.identity import normalize_kanbanlan_id


class KanbanlanStatus(StrEnum):
    INBOX = "Inbox"
    READY = "Ready"
    IN_PROGRESS = "In progress"
    BLOCKED = "Blocked"
    IN_REVIEW = "In review"
    DONE = "Done"


@dataclass(frozen=True)
class KanbanlanRequest:
    kanbanlan_id: str | None
    provider: str
    provider_id: str | None
    provider_ref: str
    display_id: str
    title: str
    body: str
    url: str | None
    status: str | None
    state: str | None
    priority: str | None
    active_claim: dict[str, Any] | None
    linked_open_pull_requests: tuple[dict[str, Any], ...]
    number: int | None = None
    repository: str | None = None

    @classmethod
    def from_snapshot_item(cls, item: dict[str, Any]) -> KanbanlanRequest:
        number = item.get("number")
        display_id = item.get("display_id") or (f"#{number}" if number is not None else "unknown")
        provider_ref = item.get("provider_ref") or display_id
        return cls(
            kanbanlan_id=item.get("kanbanlan_id"),
            provider=item.get("provider", "github"),
            provider_id=item.get("provider_id") or item.get("content_id"),
            provider_ref=provider_ref,
            display_id=display_id,
            title=item.get("title") or "Untitled request",
            body=item.get("body") or "",
            url=item.get("canonical_url") or item.get("url"),
            status=item.get("status"),
            state=item.get("state"),
            priority=item.get("priority"),
            active_claim=item.get("active_claim"),
            linked_open_pull_requests=tuple(item.get("linked_open_pull_requests", [])),
            number=number,
            repository=item.get("repository"),
        )

    def matches(self, reference: str | int, *, local_repository: str | None = None) -> bool:
        """Report whether ``reference`` names this request.

        A bare issue number is only ever a local reference. When
        ``local_repository`` is known, a bare number must not match a peer
        repository's identically numbered request.
        """

        value = str(reference).strip()
        normalized = normalize_kanbanlan_id(value)
        if normalized:
            return normalized == self.kanbanlan_id
        folded = value.casefold()
        candidates = {self.display_id.casefold(), self.provider_ref.casefold()}
        if self.repository and self.number is not None:
            qualified = f"{self.repository}#{self.number}".casefold()
            candidates.update({qualified, f"{self.provider}:{qualified}"})
        if folded in candidates:
            return True
        if self.number is not None and folded in {str(self.number), f"#{self.number}"}:
            return (
                local_repository is None
                or self.repository is None
                or self.repository == local_repository
            )
        return False

    @property
    def label(self) -> str:
        if self.kanbanlan_id:
            return f"{self.kanbanlan_id} ({self.display_id})"
        return self.display_id


def resolve_request_item(snapshot: dict[str, Any], reference: str | int) -> dict[str, Any]:
    local_repository = snapshot.get("source", {}).get("repository")
    matches = [
        item
        for item in snapshot.get("items", [])
        if item.get("type") == "ISSUE"
        and KanbanlanRequest.from_snapshot_item(item).matches(
            reference, local_repository=local_repository
        )
    ]
    if not matches:
        raise RuntimeError(f"request {reference!r} is not on the configured kanban home")
    if len(matches) > 1:
        raise RuntimeError(f"request reference {reference!r} is ambiguous")
    return matches[0]


def request_label(item: dict[str, Any]) -> str:
    return KanbanlanRequest.from_snapshot_item(item).label
