from __future__ import annotations

import unittest

from kanbanlan.workflow import expected_state, plan_reconciliation


def item(**overrides):
    value = {
        "type": "ISSUE",
        "number": 1,
        "title": "Example",
        "url": "url",
        "state": "OPEN",
        "status": "Ready",
        "labels": [{"name": "status:ready"}],
        "linked_open_pull_requests": [],
        "active_claim": None,
        "project_item_id": "item-1",
    }
    value.update(overrides)
    return value


class WorkflowTests(unittest.TestCase):
    def test_claim_and_pull_request_override_labels(self) -> None:
        self.assertEqual(
            ("status:in-progress", "In progress", "active CLAIM comment exists"),
            expected_state(item(active_claim={"session": "one"})),
        )
        self.assertEqual(
            ("status:review", "In review", "linked pull request is open"),
            expected_state(
                item(
                    active_claim={"session": "one"},
                    linked_open_pull_requests=[{"number": 2}],
                )
            ),
        )

    def test_closed_issue_is_done_without_status_label(self) -> None:
        self.assertEqual(
            (None, "Done", "issue is closed"),
            expected_state(item(state="CLOSED")),
        )

    def test_plan_adds_missing_issue_and_repairs_drift(self) -> None:
        snapshot = {"items": [item(status="Inbox")]}
        open_issues = [
            {"number": 1, "url": "one"},
            {"number": 2, "url": "two"},
        ]
        drift = plan_reconciliation(snapshot, open_issues)
        self.assertEqual(
            ["add_to_project", "set_project_status"],
            [value.kind for value in drift],
        )
