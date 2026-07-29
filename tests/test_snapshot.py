from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kanbanlan.config import Config
from kanbanlan.sessions import AgentSession, activity_comment
from kanbanlan.snapshot import CacheStore, active_claim, build_snapshot, isoformat


def config() -> Config:
    return Config(
        repository="acme/widget",
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def issue_item(
    number: int,
    status: str,
    labels: list[str],
    *,
    comments: list[dict] | None = None,
    state: str = "OPEN",
) -> dict:
    return {
        "id": f"item-{number}",
        "type": "ISSUE",
        "isArchived": False,
        "fieldValues": {
            "nodes": [{"name": status, "field": {"name": "Status"}}],
        },
        "content": {
            "id": f"issue-{number}",
            "number": number,
            "title": f"Issue {number}",
            "body": "Touchpoints: docs",
            "url": f"https://github.test/issues/{number}",
            "state": state,
            "stateReason": None,
            "createdAt": "2026-07-25T00:00:00Z",
            "updatedAt": "2026-07-25T01:00:00Z",
            "closedAt": None,
            "repository": {"nameWithOwner": "acme/widget"},
            "labels": {"nodes": [{"name": name, "color": "000000"} for name in labels]},
            "assignees": {"nodes": []},
            "comments": {"nodes": comments or []},
        },
    }


class SnapshotTests(unittest.TestCase):
    def test_session_history_is_normalized_and_comment_limit_is_reported(self) -> None:
        body = activity_comment(
            action="capture",
            at="2026-07-29T12:00:00Z",
            from_status=None,
            to_status="Inbox",
            actor=AgentSession("codex", "019f-test", "test"),
        )
        item = issue_item(
            1,
            "Inbox",
            ["priority:p2", "status:intake"],
            comments=[
                {
                    "body": body,
                    "createdAt": "2026-07-29T12:00:01Z",
                    "author": {"login": "agent-user"},
                }
            ],
        )
        item["content"]["comments"]["totalCount"] = 101
        project = {
            "id": "project",
            "number": 2,
            "title": "Delivery",
            "url": "url",
            "fields": {"nodes": []},
            "items": [item],
        }

        snapshot = build_snapshot(config(), project, [], {}, datetime.now(UTC))
        normalized = snapshot["items"][0]

        self.assertEqual("019f-test · codex", normalized["session_history"][0]["actor"]["display"])
        self.assertTrue(normalized["session_history_truncated"])

    def test_ready_cards_are_sorted_by_priority_then_number(self) -> None:
        project = {
            "id": "project",
            "number": 2,
            "title": "Delivery",
            "url": "url",
            "fields": {"nodes": []},
            "items": [
                issue_item(30, "Ready", ["priority:p2", "status:ready"]),
                issue_item(20, "Ready", ["priority:p0", "status:ready"]),
                issue_item(10, "Ready", ["priority:p0", "status:ready"]),
                issue_item(5, "Blocked", ["priority:p0", "status:blocked"]),
            ],
        }
        snapshot = build_snapshot(
            config(),
            project,
            [],
            {},
            datetime(2026, 7, 25, tzinfo=UTC),
        )
        self.assertEqual([10, 20, 30], [item["number"] for item in snapshot["ready_cards"]])
        self.assertEqual(10, snapshot["next_ready"]["number"])

    def test_losing_release_does_not_clear_winning_claim(self) -> None:
        comments = [
            {
                "body": "CLAIM: 2026-07-25T01:00:00Z\nSession: winner",
                "createdAt": "2026-07-25T01:00:00Z",
                "author": {"login": "same-account"},
            },
            {
                "body": "CLAIM: 2026-07-25T01:00:01Z\nSession: loser",
                "createdAt": "2026-07-25T01:00:01Z",
                "author": {"login": "same-account"},
            },
            {
                "body": "RELEASED: 2026-07-25T01:00:02Z — lost\nSession: loser",
                "createdAt": "2026-07-25T01:00:02Z",
                "author": {"login": "same-account"},
            },
        ]
        self.assertEqual("winner", active_claim(comments)["session"])


class CacheTests(unittest.TestCase):
    def test_refresh_is_private_atomic_and_preserves_last_good_data(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.error = False

            def fetch(self):
                if self.error:
                    raise RuntimeError("offline")
                return (
                    {
                        "id": "project",
                        "number": 2,
                        "title": "Delivery",
                        "url": "url",
                        "fields": {"nodes": []},
                        "items": [],
                    },
                    [],
                    {},
                )

        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            client = Client()
            store.refresh(client)
            original = store.snapshot_path.read_text(encoding="utf-8")
            client.error = True
            with self.assertRaises(RuntimeError):
                store.refresh(client)

            self.assertEqual(original, store.snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(0o600, os.stat(store.snapshot_path).st_mode & 0o777)
            health = json.loads(store.health_path.read_text(encoding="utf-8"))
            self.assertEqual("error", health["refresh_status"])

    def test_old_snapshot_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            store._write_json(
                store.snapshot_path,
                {
                    "generated_at": isoformat(datetime.now(UTC) - timedelta(seconds=181)),
                },
            )
            self.assertFalse(store.is_fresh())
