from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config
from kanbanlan.runner import RateLimitError
from kanbanlan.sessions import AgentSession, activity_comment
from kanbanlan.snapshot import (
    SCHEMA_VERSION,
    CacheStore,
    active_claim,
    build_snapshot,
    isoformat,
)


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


def _stale_snapshot(remaining: int, reset_in_seconds: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": isoformat(datetime.now(UTC) - timedelta(seconds=1000)),
        "rate_limit": {
            "remaining": remaining,
            "resetAt": isoformat(datetime.now(UTC) + timedelta(seconds=reset_in_seconds)),
        },
    }


class _CountingClient:
    """Fetch-only client, so ``hasattr(client, "snapshot")`` stays False."""

    def __init__(self, rate_limit: dict | None = None, error: Exception | None = None) -> None:
        self.fetches = 0
        self.rate_limit = rate_limit or {}
        self.error = error

    def fetch(self):
        self.fetches += 1
        if self.error:
            raise self.error
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
            self.rate_limit,
        )


class RateLimitBehaviorTests(unittest.TestCase):
    def test_ensure_reuses_a_refresh_completed_while_waiting_for_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            store._write_json(store.snapshot_path, _stale_snapshot(4000, 1800))
            fresh = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": isoformat(datetime.now(UTC)),
                "rate_limit": {},
            }

            class RefreshedWhileWaiting:
                def __init__(self, path, timeout: float = 10.0) -> None:
                    pass

                def __enter__(self):
                    store._write_json(store.snapshot_path, fresh)
                    return self

                def __exit__(self, *args) -> None:
                    pass

            client = _CountingClient()
            with mock.patch("kanbanlan.snapshot.FileLock", RefreshedWhileWaiting):
                result = store.ensure(client)

            self.assertEqual(0, client.fetches)
            self.assertEqual(fresh["generated_at"], result["generated_at"])

    def test_rate_limited_refresh_serves_the_last_good_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            stale = _stale_snapshot(4000, 1800)
            store._write_json(store.snapshot_path, stale)
            client = _CountingClient(error=RateLimitError("API rate limit exceeded"))

            result = store.ensure(client)

            self.assertEqual(1, client.fetches)
            self.assertEqual(stale["generated_at"], result["generated_at"])
            health = json.loads(store.health_path.read_text(encoding="utf-8"))
            self.assertEqual("throttled", health["refresh_status"])
            self.assertEqual("RateLimitError", health["error"]["kind"])

    def test_explicit_refresh_still_fails_when_rate_limited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            store._write_json(store.snapshot_path, _stale_snapshot(4000, 1800))
            client = _CountingClient(error=RateLimitError("API rate limit exceeded"))

            with self.assertRaises(RateLimitError):
                store.refresh(client)

    def test_rate_limited_refresh_without_usable_snapshot_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            client = _CountingClient(error=RateLimitError("API rate limit exceeded"))

            with self.assertRaises(RateLimitError):
                store.ensure(client)

    def test_low_remaining_defers_refresh_until_the_reset_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            low = _stale_snapshot(10, 1800)
            store._write_json(store.snapshot_path, low)
            client = _CountingClient(rate_limit={"remaining": 4999, "resetAt": "soon"})

            deferred = store.ensure(client)
            self.assertEqual(0, client.fetches)
            self.assertEqual(low["generated_at"], deferred["generated_at"])

            after_reset = _stale_snapshot(10, -5)
            store._write_json(store.snapshot_path, after_reset)
            refreshed = store.ensure(client)
            self.assertEqual(1, client.fetches)
            self.assertNotEqual(after_reset["generated_at"], refreshed["generated_at"])

    def test_zero_floor_disables_the_deferral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            no_floor = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=2,
                rate_limit_floor=0,
            )
            store = CacheStore(no_floor, Path(directory))
            store._write_json(store.snapshot_path, _stale_snapshot(10, 1800))
            client = _CountingClient()

            store.ensure(client)

            self.assertEqual(1, client.fetches)

    def test_malformed_rate_limit_data_never_crashes_and_never_defers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            malformed: list[dict] = [
                {"remaining": 10, "resetAt": "2099-08-21T12:00:00"},  # timezone-naive
                {"remaining": 10, "resetAt": "not-a-time"},
                {"remaining": 10, "resetAt": None},
                {"remaining": 10, "resetAt": 12345},
                {"remaining": True, "resetAt": "2099-08-21T12:00:00Z"},
                {"remaining": "10", "resetAt": "2099-08-21T12:00:00Z"},
            ]
            for rate_limit in malformed:
                with self.subTest(rate_limit=rate_limit):
                    low = _stale_snapshot(10, 1800)
                    low["rate_limit"] = rate_limit
                    store._write_json(store.snapshot_path, low)
                    client = _CountingClient()

                    store.ensure(client)

                    self.assertEqual(1, client.fetches)

    def test_old_schema_snapshot_is_never_served_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            outdated = _stale_snapshot(10, 1800)
            outdated["schema_version"] = SCHEMA_VERSION - 1
            store._write_json(store.snapshot_path, outdated)
            client = _CountingClient(error=RateLimitError("API rate limit exceeded"))

            with self.assertRaises(RateLimitError):
                store.ensure(client)
