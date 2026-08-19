"""Plan and apply a move of one canonical request to another repository.

A rehome moves the request, not its implementation. Branches, worktrees,
commits, and pull requests stay where they are, and the immutable Kanbanlan ID
never changes: that identity is the whole reason a move is preferable to
recreating the request somewhere else.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from kanbanlan.domain import KanbanlanRequest
from kanbanlan.snapshot import qualified_reference

# Fields GitHub carries through an issue transfer, and fields it does not.
PRESERVED_FIELDS = (
    "kanbanlan_id",
    "title",
    "body",
    "comments",
    "session_history",
    "assignees",
    "state",
)
TRANSFERABLE_LABEL_PREFIXES = ("status:", "priority:")


@dataclass(frozen=True)
class Blocker:
    """One reason a move must not proceed without an explicit safe sequence."""

    kind: str
    detail: str
    resolution: str


@dataclass(frozen=True)
class RehomePlan:
    kanbanlan_id: str
    source_repository: str
    source_provider_ref: str
    source_url: str | None
    source_number: int
    target_repository: str
    status: str | None
    priority: str | None
    preserved: tuple[str, ...] = PRESERVED_FIELDS
    dropped: tuple[dict[str, str], ...] = ()
    target_link_required: bool = False
    labels_to_provision: tuple[str, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kanbanlan_id": self.kanbanlan_id,
            "source": {
                "repository": self.source_repository,
                "provider_ref": self.source_provider_ref,
                "url": self.source_url,
                "number": self.source_number,
            },
            "target": {
                "repository": self.target_repository,
                "link_required": self.target_link_required,
                "labels_to_provision": list(self.labels_to_provision),
            },
            "status": self.status,
            "priority": self.priority,
            "preserved": list(self.preserved),
            "dropped": [dict(value) for value in self.dropped],
            "blockers": [asdict(value) for value in self.blockers],
            "warnings": list(self.warnings),
            "blocked": self.blocked,
        }


def plan_rehome(
    item: dict[str, Any],
    target_repository: str,
    inspection: dict[str, Any],
) -> RehomePlan:
    """Describe the move without performing any part of it."""

    request = KanbanlanRequest.from_snapshot_item(item)
    if not request.kanbanlan_id:
        raise RuntimeError(
            "request has no portable identity; run 'kanbanlan reconcile --apply' first"
        )
    source_repository = request.repository or ""
    blockers: list[Blocker] = []
    if source_repository == target_repository:
        blockers.append(
            Blocker(
                kind="same_repository",
                detail=f"the request already lives in {target_repository}",
                resolution="name a different repository",
            )
        )
    if request.state != "OPEN":
        blockers.append(
            Blocker(
                kind="request_not_open",
                detail=f"the request is {request.state or 'unknown'}",
                resolution=(
                    "reopen the request before moving it, or leave it where it was delivered"
                ),
            )
        )
    claim = request.active_claim
    if claim:
        owner = claim.get("session") or claim.get("author") or "an active session"
        blockers.append(
            Blocker(
                kind="active_claim",
                detail=f"{owner} holds an active claim",
                resolution=(
                    "release the claim with "
                    f"'kanbanlan release {request.kanbanlan_id} --reason \"rehoming\"' "
                    "and reclaim it in the target repository"
                ),
            )
        )
    for pull_request in request.linked_open_pull_requests:
        blockers.append(
            Blocker(
                kind="linked_open_pull_request",
                detail=f"{pull_request['provider_ref']} is open and linked",
                resolution=(
                    "merge or close the pull request first; a rehome moves the request, "
                    "not its implementation"
                ),
            )
        )

    dropped: list[dict[str, str]] = []
    if item.get("milestone"):
        dropped.append(
            {
                "field": "milestone",
                "value": str(item["milestone"]),
                "reason": "GitHub does not carry a milestone across repositories",
            }
        )
    for label in item.get("labels", []):
        name = label["name"]
        if name.startswith(TRANSFERABLE_LABEL_PREFIXES):
            continue
        dropped.append(
            {
                "field": "label",
                "value": name,
                "reason": "labels absent from the target repository are dropped on transfer",
            }
        )

    warnings = [
        "branches, worktrees, commits, and pull requests are not moved",
        f"the request keeps {request.kanbanlan_id} and receives a new issue number",
    ]
    return RehomePlan(
        kanbanlan_id=request.kanbanlan_id,
        source_repository=source_repository,
        source_provider_ref=request.provider_ref,
        source_url=request.url,
        source_number=item["number"],
        target_repository=inspection["repository"],
        status=request.status,
        priority=request.priority,
        dropped=tuple(dropped),
        target_link_required=not inspection["already_linked"],
        labels_to_provision=tuple(inspection["missing_labels"]),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def rehome_result(plan: RehomePlan, item: dict[str, Any]) -> dict[str, Any]:
    """Report the applied move, naming both provider references."""

    request = KanbanlanRequest.from_snapshot_item(item)
    payload = plan.to_dict()
    payload["applied"] = True
    payload["target"].update(
        {
            "provider_ref": request.provider_ref
            or qualified_reference(request.repository, request.number),
            "number": request.number,
            "url": request.url,
        }
    )
    payload["status"] = request.status
    payload["priority"] = request.priority
    return payload


def format_plan(plan: RehomePlan) -> list[str]:
    lines = [
        f"{plan.kanbanlan_id}: {plan.source_provider_ref} -> {plan.target_repository}",
        f"  status: {plan.status or 'unspecified'}",
        f"  priority: {plan.priority or 'unprioritized'}",
        f"  preserved: {', '.join(plan.preserved)}",
    ]
    if plan.target_link_required:
        lines.append(f"  will link {plan.target_repository} to the configured Project")
    if plan.labels_to_provision:
        lines.append(f"  will provision labels: {', '.join(plan.labels_to_provision)}")
    for value in plan.dropped:
        lines.append(f"  dropped {value['field']} {value['value']}: {value['reason']}")
    for value in plan.warnings:
        lines.append(f"  note: {value}")
    for blocker in plan.blockers:
        lines.append(f"  blocked ({blocker.kind}): {blocker.detail}; {blocker.resolution}")
    return lines
