from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kanbanlan.github import GitHub
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
    issue_url: str | None = None
    project_item_id: str | None = None


def expected_state(item: dict[str, Any]) -> tuple[str | None, str, str]:
    if item.get("state") == "CLOSED":
        return None, "Done", "issue is closed"
    if item.get("linked_open_pull_requests"):
        return "status:review", "In review", "linked pull request is open"
    if item.get("active_claim"):
        return "status:in-progress", "In progress", "active CLAIM comment exists"

    status_labels = [
        label["name"] for label in item.get("labels", []) if label["name"] in LABEL_TO_STATUS
    ]
    if len(status_labels) == 1:
        label = status_labels[0]
        return label, LABEL_TO_STATUS[label], "issue status label"

    project_status = item.get("status")
    if project_status in STATUS_TO_LABEL and project_status != "Done":
        return (
            STATUS_TO_LABEL[project_status],
            project_status,
            "Project Status resolves missing or conflicting labels",
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
    for issue in open_issues:
        if issue["number"] not in issue_items:
            drift.append(
                Drift(
                    kind="add_to_project",
                    issue_number=issue["number"],
                    current=None,
                    expected="Project item",
                    detail="open repository issue is missing from the Project",
                    issue_url=issue.get("url"),
                )
            )

    for number, item in issue_items.items():
        expected_label, expected_status, reason = expected_state(item)
        current_labels = sorted(
            label["name"] for label in item.get("labels", []) if label["name"] in LABEL_TO_STATUS
        )
        wanted_labels = [expected_label] if expected_label else []
        if current_labels != wanted_labels:
            drift.append(
                Drift(
                    kind="set_issue_label",
                    issue_number=number,
                    current=", ".join(current_labels) or None,
                    expected=expected_label,
                    detail=reason,
                    issue_url=item.get("url"),
                    project_item_id=item.get("project_item_id"),
                )
            )
        if item.get("status") != expected_status:
            drift.append(
                Drift(
                    kind="set_project_status",
                    issue_number=number,
                    current=item.get("status"),
                    expected=expected_status,
                    detail=reason,
                    issue_url=item.get("url"),
                    project_item_id=item.get("project_item_id"),
                )
            )
    return sorted(
        drift,
        key=lambda value: (
            0 if value.kind == "add_to_project" else 1,
            value.issue_number,
            value.kind,
        ),
    )


def apply_reconciliation(
    github: GitHub,
    store: CacheStore,
    snapshot: dict[str, Any],
    open_issues: list[dict[str, Any]],
) -> tuple[list[Drift], dict[str, Any]]:
    planned = plan_reconciliation(snapshot, open_issues)
    additions = [value for value in planned if value.kind == "add_to_project"]
    for action in additions:
        assert action.issue_url
        github.add_issue_to_project(action.issue_url)
    if additions:
        snapshot = store.refresh(github)
        planned = plan_reconciliation(snapshot, open_issues)

    for action in planned:
        if action.kind == "set_issue_label":
            github.set_issue_status_label(action.issue_number, action.expected)
        elif action.kind == "set_project_status":
            if not action.project_item_id or not action.expected:
                raise RuntimeError(f"issue #{action.issue_number} has no editable Project item")
            github.set_project_status(
                action.project_item_id,
                snapshot["project"],
                action.expected,
            )
    refreshed = store.refresh(github)
    remaining = plan_reconciliation(refreshed, github.open_issues())
    return remaining, refreshed


def format_drift(value: Drift) -> str:
    current = value.current or "none"
    expected = value.expected or "none"
    return f"#{value.issue_number} {value.kind}: {current} -> {expected} ({value.detail})"
