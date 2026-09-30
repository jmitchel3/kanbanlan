from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config
from kanbanlan.runner import CommandError, CommandResult, RateLimitError
from kanbanlan.snapshot import SCHEMA_VERSION, CacheStore, isoformat
from kanbanlan.workflow import expected_state, plan_reconciliation, read_board


def item(**overrides):
    value = {
        "type": "ISSUE",
        "kanbanlan_id": "KBL-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
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
    def test_low_quota_stops_both_reconciliation_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(Config("acme/widget", "acme", "organization", 2), Path(directory))
            snapshot = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": isoformat(datetime.now(UTC)),
                "rate_limit": {
                    "remaining": 10,
                    "resetAt": isoformat(datetime.now(UTC) + timedelta(minutes=20)),
                },
            }
            store._write_json(store.snapshot_path, snapshot)
            provider = mock.Mock()

            with self.assertRaises(RateLimitError):
                read_board(store, provider)

            self.assertEqual([], provider.mock_calls)
            self.assertEqual(snapshot, store.snapshot())

    def test_deferred_read_keeps_githubs_own_refusal_on_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(Config("acme/widget", "acme", "organization", 2), Path(directory))
            reset_at = isoformat(datetime.now(UTC) + timedelta(minutes=5))
            store.record_rate_limit(RateLimitError("secondary rate limit", reset_at=reset_at))
            recorded = store.inspect()["error"]
            provider = mock.Mock()

            for _ in range(2):
                with self.assertRaises(RateLimitError) as raised:
                    read_board(store, provider)
                self.assertTrue(raised.exception.deferred)

            self.assertEqual(recorded, store.inspect()["error"])
            self.assertEqual([], provider.mock_calls)

    def test_issue_list_rate_limit_is_classified_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(Config("acme/widget", "acme", "organization", 2), Path(directory))
            provider = mock.Mock()
            provider.list_open_requests.side_effect = CommandError(
                CommandResult(
                    ("gh", "issue", "list"),
                    1,
                    "",
                    "gh: API rate limit already exceeded for user ID 259989802.",
                )
            )
            with mock.patch.object(store, "refresh", return_value={"items": []}):
                with self.assertRaises(RateLimitError) as raised:
                    read_board(store, provider)

            self.assertIsNotNone(raised.exception.reset_at)
            self.assertEqual("throttled", store.inspect()["refresh_status"])
            with self.assertRaises(RateLimitError):
                read_board(store, provider)
            provider.list_open_requests.assert_called_once()

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
            (None, "Done", "canonical request is closed"),
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
            ["add_to_projection", "set_projection_status"],
            [value.kind for value in drift],
        )
