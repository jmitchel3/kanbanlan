from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kanbanlan.domain import KanbanlanRequest
from kanbanlan.identity import new_kanbanlan_id
from kanbanlan.providers import CoordinationProvider
from kanbanlan.snapshot import CacheStore

LABEL_TO_STATUS = {
    "status:intake": "Inbox",
    "status:ready": "Ready",
    "status:in-progress": "In progress",
    "status:blocked": "Blocked",
    "status:review": "In review",
}
STATUS_TO_LABEL = {value: key for key, value in LABEL_TO_STATUS.items()}


@dataclass(frozen=True)
class Drift:
    kind: str
    issue_number: int
    current: str | None
    expected: str | None
    detail: str
    kanbanlan_id: str | None = None
    issue_url: str | None = None
    project_item_id: str | None = None


def expected_state(item: dict[str, Any]) -> tuple[str | None, str, str]:
    request = KanbanlanRequest.from_snapshot_item(item)
    if request.state == "CLOSED":
        return None, "Done", "canonical request is closed"
    if request.linked_open_pull_requests:
        return "status:review", "In review", "linked pull request is open"
    if request.active_claim:
        return "status:in-progress", "In progress", "active CLAIM comment exists"

    status_labels = [
        label["name"] for label in item.get("labels", []) if label["name"] in LABEL_TO_STATUS
    ]
    if len(status_labels) == 1:
        label = status_labels[0]
        return label, LABEL_TO_STATUS[label], "canonical request status"

    project_status = item.get("status")
    if project_status in STATUS_TO_LABEL and project_status != "Done":
        return (
            STATUS_TO_LABEL[project_status],
            project_status,
            "projection status resolves missing or conflicting canonical status",
        )
    return "status:intake", "Inbox", "default for an open unclassified issue"


def plan_reconciliation(
    snapshot: dict[str, Any],
    open_issues: list[dict[str, Any]],
) -> list[Drift]:
    issue_items = {
        item["number"]: item for item in snapshot.get("items", []) if item.get("type") == "ISSUE"
    }
    drift: list[Drift] = []
    identity_owners: dict[str, list[int]] = {}
    for number, item in issue_items.items():
        if item.get("kanbanlan_id"):
            identity_owners.setdefault(item["kanbanlan_id"], []).append(number)
    for kanbanlan_id, numbers in identity_owners.items():
        if len(numbers) < 2:
            continue
        for number in numbers:
            item = issue_items[number]
            other_references = ", ".join(f"#{value}" for value in numbers if value != number)
            drift.append(
                Drift(
                    kind="duplicate_kanbanlan_id",
                    issue_number=number,
                    current=kanbanlan_id,
                    expected="unique Kanbanlan ID",
                    detail=f"identity is also used by {other_references}",
                    kanbanlan_id=kanbanlan_id,
                    issue_url=item.get("url"),
                    project_item_id=item.get("project_item_id"),
                )
            )
    for issue in open_issues:
        if issue["number"] not in issue_items:
            drift.append(
                Drift(
                    kind="add_to_projection",
                    issue_number=issue["number"],
                    current=None,
                    expected="projection item",
                    detail="open canonical request is missing from the configured projection",
                    issue_url=issue.get("url"),
                )
            )

    for number, item in issue_items.items():
        if not item.get("kanbanlan_id"):
            drift.append(
                Drift(
                    kind="assign_kanbanlan_id",
                    issue_number=number,
                    current=None,
                    expected="new Kanbanlan ID",
                    detail="request has no portable identity",
                    issue_url=item.get("url"),
                    project_item_id=item.get("project_item_id"),
                )
            )
        expected_label, expected_status, reason = expected_state(item)
        current_labels = sorted(
            label["name"] for label in item.get("labels", []) if label["name"] in LABEL_TO_STATUS
        )
        wanted_labels = [expected_label] if expected_label else []
        if current_labels != wanted_labels:
            drift.append(
                Drift(
                    kind="set_request_status",
                    issue_number=number,
                    current=", ".join(current_labels) or None,
                    expected=expected_label,
                    detail=reason,
                    kanbanlan_id=item.get("kanbanlan_id"),
                    issue_url=item.get("url"),
                    project_item_id=item.get("project_item_id"),
                )
            )
        if item.get("status") != expected_status:
            drift.append(
                Drift(
                    kind="set_projection_status",
                    issue_number=number,
                    current=item.get("status"),
                    expected=expected_status,
                    detail=reason,
                    kanbanlan_id=item.get("kanbanlan_id"),
                    issue_url=item.get("url"),
                    project_item_id=item.get("project_item_id"),
                )
            )
    return sorted(
        drift,
        key=lambda value: (
            {
                "add_to_projection": 0,
                "assign_kanbanlan_id": 1,
                "duplicate_kanbanlan_id": 1,
            }.get(value.kind, 2),
            value.issue_number,
            value.kind,
        ),
    )


def apply_reconciliation(
    provider: CoordinationProvider,
    store: CacheStore,
    snapshot: dict[str, Any],
    open_issues: list[dict[str, Any]],
) -> tuple[list[Drift], dict[str, Any]]:
    planned = plan_reconciliation(snapshot, open_issues)
    additions = [value for value in planned if value.kind == "add_to_projection"]
    for action in additions:
        assert action.issue_url
        provider.add_to_projection(action.issue_url)
    if additions:
        snapshot = store.refresh(provider)
        planned = plan_reconciliation(snapshot, open_issues)

    identities = [value for value in planned if value.kind == "assign_kanbanlan_id"]
    for action in identities:
        provider.ensure_request_identity(action.issue_number, new_kanbanlan_id())
    if identities:
        snapshot = store.refresh(provider)
        planned = plan_reconciliation(snapshot, open_issues)

    for action in planned:
        if action.kind == "set_request_status":
            provider.set_request_status(action.issue_number, action.expected)
        elif action.kind == "set_projection_status":
            if not action.project_item_id or not action.expected:
                raise RuntimeError(
                    f"request #{action.issue_number} has no editable projection item"
                )
            provider.set_projection_status(
                action.project_item_id,
                snapshot["project"],
                action.expected,
            )
    refreshed = store.refresh(provider)
    remaining = plan_reconciliation(refreshed, provider.list_open_requests())
    return remaining, refreshed


def format_drift(value: Drift) -> str:
    current = value.current or "none"
    expected = value.expected or "none"
    reference = value.kanbanlan_id or f"#{value.issue_number}"
    return f"{reference} {value.kind}: {current} -> {expected} ({value.detail})"
