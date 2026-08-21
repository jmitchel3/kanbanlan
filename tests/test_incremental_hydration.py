from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config
from kanbanlan.github import (
    ITEM_CACHE_MAX_AGE_SECONDS,
    ITEM_CACHE_SCHEMA_VERSION,
    PROBE_ITEM_FIELDS,
    GitHub,
)
from kanbanlan.runner import RateLimitError


def config() -> Config:
    return Config(
        repository="acme/widget",
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


PROJECT_METADATA = {
    "id": "PVT_1",
    "number": 2,
    "title": "Delivery",
    "url": "https://github.com/orgs/acme/projects/2",
    "updatedAt": "2026-01-05T00:00:00Z",
    "repositories": {
        "pageInfo": {"hasNextPage": False},
        "nodes": [{"nameWithOwner": "acme/widget"}],
    },
    "fields": {
        "nodes": [
            {
                "id": "field-status",
                "name": "Status",
                "dataType": "SINGLE_SELECT",
                "options": [{"id": "opt-ready", "name": "Ready", "color": "GREEN"}],
            }
        ]
    },
}


def full_item(
    item_id: str,
    number: int,
    *,
    item_updated: str,
    content_updated: str,
    title: str = "A card",
    comments: int = 0,
) -> dict:
    """Build one raw item node in the exact shape PROJECT_QUERY returns."""

    return {
        "id": item_id,
        "type": "ISSUE",
        "isArchived": False,
        "updatedAt": item_updated,
        "fieldValues": {
            "nodes": [
                {
                    "name": "Ready",
                    "optionId": "opt-ready",
                    "field": {"id": "field-status", "name": "Status"},
                }
            ]
        },
        "content": {
            "id": f"I_{item_id}",
            "number": number,
            "title": title,
            "body": "",
            "url": f"https://github.com/acme/widget/issues/{number}",
            "state": "OPEN",
            "stateReason": None,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": content_updated,
            "closedAt": None,
            "labels": {"nodes": []},
            "milestone": None,
            "assignees": {"nodes": []},
            "comments": {"totalCount": comments, "nodes": []},
            "repository": {"nameWithOwner": "acme/widget"},
        },
    }


def draft_item(item_id: str, *, item_updated: str) -> dict:
    return {
        "id": item_id,
        "type": "DRAFT_ISSUE",
        "isArchived": False,
        "updatedAt": item_updated,
        "fieldValues": {"nodes": []},
        "content": {
            "id": f"D_{item_id}",
            "title": "A draft",
            "body": "",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": item_updated,
        },
    }


def probe_view(node: dict) -> dict:
    """Reduce a full item node to what the probe query would return for it.

    test_probe_query_requests_exactly_what_this_mirror_assumes pins this
    shape to the real PROBE_ITEM_FIELDS constant, so query drift fails the
    suite instead of silently desynchronizing the stub.
    """

    content = node.get("content") or {}
    probe: dict = {
        "id": node["id"],
        "type": node["type"],
        "isArchived": node["isArchived"],
        "updatedAt": node["updatedAt"],
        "content": {},
    }
    if node["type"] in {"ISSUE", "PULL_REQUEST"}:
        probe["content"] = {
            "id": content.get("id"),
            "number": content.get("number"),
            "updatedAt": content.get("updatedAt"),
            "repository": content.get("repository"),
        }
    if node["type"] == "ISSUE":
        probe["content"]["comments"] = {
            "totalCount": (content.get("comments") or {}).get("totalCount")
        }
    return probe


class StubGitHub(GitHub):
    """A GitHub whose graphql dispatches on query text against a fake board.

    The board is the list of full item nodes GitHub would hold; the stub
    serves the full page query, the probe query, and the hydration query from
    that one source of truth, and counts each so tests can assert which path
    a refresh took and what it cost.
    """

    def __init__(self, items: list[dict], cache_path: Path):
        super().__init__(Path("/tmp"), config())
        self.items = items
        self.cache_path = cache_path
        self.metadata = copy.deepcopy(PROJECT_METADATA)
        self.full_pages = 0
        self.probe_pages = 0
        self.hydration_batches: list[list[str]] = []
        self.deleted_hydration_ids: set[str] = set()
        self.hydration_error: Exception | None = None
        self.page_size: int | None = None

    def _item_cache_path(self) -> Path:
        return self.cache_path

    def graphql(self, query, variables, *, retry=False):
        rate_limit = {"cost": 1, "remaining": 4000, "resetAt": "2026-01-01T01:00:00Z"}
        if "nodes(ids:" in query:
            ids = list(variables["ids"])
            self.hydration_batches.append(ids)
            if self.hydration_error is not None:
                raise self.hydration_error
            if self.deleted_hydration_ids & set(ids):
                # GitHub reports a deleted id as a null node plus an entry in
                # the errors array, and graphql() raises on any errors array.
                raise RuntimeError("Could not resolve to a node with the global id")
            by_id = {node["id"]: node for node in self.items}
            nodes = [copy.deepcopy(by_id.get(item_id)) for item_id in ids]
            return {"nodes": nodes, "rateLimit": rate_limit}
        assert "projectV2" in query
        probing = "comments(last: 100)" not in query
        if probing:
            self.probe_pages += 1
            nodes = [probe_view(node) for node in self.items]
        else:
            self.full_pages += 1
            nodes = copy.deepcopy(self.items)
        start = int(variables.get("after") or 0)
        if self.page_size is not None:
            page = nodes[start : start + self.page_size]
            has_next = start + self.page_size < len(nodes)
            cursor = str(start + self.page_size)
        else:
            page, has_next, cursor = nodes, False, None
        return {
            "organization": {
                "projectV2": {
                    **copy.deepcopy(self.metadata),
                    "items": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                        "nodes": page,
                    },
                }
            },
            "rateLimit": rate_limit,
        }

    def reset_counters(self) -> None:
        self.full_pages = 0
        self.probe_pages = 0
        self.hydration_batches = []


class IncrementalHydrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_path = Path(self._tmp.name) / "project_items.json"

    def board(self) -> list[dict]:
        return [
            full_item(
                "item-1",
                11,
                item_updated="2026-01-02T00:00:00Z",
                content_updated="2026-01-02T00:00:00Z",
            ),
            full_item(
                "item-2",
                12,
                item_updated="2026-01-03T00:00:00Z",
                content_updated="2026-01-03T00:00:00Z",
            ),
        ]

    def github(self, items: list[dict] | None = None) -> StubGitHub:
        return StubGitHub(items if items is not None else self.board(), self.cache_path)

    def read_cache(self) -> dict:
        return json.loads(self.cache_path.read_text(encoding="utf-8"))

    def write_cache(self, cache: dict) -> None:
        self.cache_path.write_text(json.dumps(cache), encoding="utf-8")

    def test_probe_query_requests_exactly_what_this_mirror_assumes(self) -> None:
        # probe_view fakes the probe response, so the real query must carry
        # every field the mirror emits and none of the expensive ones.
        for fragment in (
            "id",
            "type",
            "isArchived",
            "updatedAt",
            "number",
            "comments { totalCount }",
            "repository { nameWithOwner }",
        ):
            self.assertIn(fragment, PROBE_ITEM_FIELDS)
        for absent in ("labels", "assignees", "fieldValues", "body", "comments(last"):
            self.assertNotIn(absent, PROBE_ITEM_FIELDS)

    def test_first_fetch_is_full_and_seeds_the_cache(self) -> None:
        github = self.github()

        project, rate_limit = github._fetch_project()

        self.assertEqual(1, github.full_pages)
        self.assertEqual(0, github.probe_pages)
        self.assertEqual([], github.hydration_batches)
        self.assertEqual(["item-1", "item-2"], [node["id"] for node in project["items"]])
        self.assertEqual(4000, rate_limit["remaining"])
        cached = self.read_cache()
        self.assertEqual(ITEM_CACHE_SCHEMA_VERSION, cached["schema_version"])
        self.assertEqual("acme/2", cached["project"])
        self.assertTrue(cached["fields_fingerprint"])
        self.assertEqual({"item-1", "item-2"}, set(cached["items"]))
        for entry in cached["items"].values():
            self.assertTrue(entry["fetched_at"])

    def test_unchanged_board_reuses_every_node_without_hydration(self) -> None:
        github = self.github()
        full_project, _ = github._fetch_project()
        github.reset_counters()

        project, rate_limit = github._fetch_project()

        self.assertEqual(0, github.full_pages)
        self.assertEqual(1, github.probe_pages)
        self.assertEqual([], github.hydration_batches)
        self.assertEqual(full_project, project)
        self.assertEqual(4000, rate_limit["remaining"])

    def test_changed_content_hydrates_only_that_item(self) -> None:
        github = self.github()
        github._fetch_project()
        github.reset_counters()
        # A new comment bumps the issue's updatedAt but not the item's.
        github.items[1]["content"]["updatedAt"] = "2026-01-04T00:00:00Z"
        github.items[1]["content"]["comments"] = {
            "totalCount": 1,
            "nodes": [{"body": "hi", "createdAt": "2026-01-04T00:00:00Z", "author": None}],
        }

        project, _ = github._fetch_project()

        self.assertEqual(0, github.full_pages)
        self.assertEqual([["item-2"]], github.hydration_batches)
        self.assertEqual(1, project["items"][1]["content"]["comments"]["totalCount"])
        self.assertEqual(0, project["items"][0]["content"]["comments"]["totalCount"])

    def test_status_edit_on_the_item_alone_triggers_hydration(self) -> None:
        github = self.github()
        github._fetch_project()
        github.reset_counters()
        # A Status field edit bumps the item's updatedAt but not the issue's.
        github.items[0]["updatedAt"] = "2026-01-06T00:00:00Z"

        project, _ = github._fetch_project()

        self.assertEqual(0, github.full_pages)
        self.assertEqual([["item-1"]], github.hydration_batches)
        self.assertEqual("2026-01-06T00:00:00Z", project["items"][0]["updatedAt"])

    def test_deleted_comment_changes_no_timestamp_but_still_hydrates(self) -> None:
        # Comments are the claim ledger, and deleting one bumps neither the
        # item's nor the issue's updatedAt; only the totalCount moves.
        github = self.github(
            [
                full_item(
                    "item-1",
                    11,
                    item_updated="2026-01-02T00:00:00Z",
                    content_updated="2026-01-02T00:00:00Z",
                    comments=2,
                )
            ]
        )
        github._fetch_project()
        github.reset_counters()
        github.items[0]["content"]["comments"]["totalCount"] = 1

        github._fetch_project()

        self.assertEqual(0, github.full_pages)
        self.assertEqual([["item-1"]], github.hydration_batches)

    def test_status_option_rename_invalidates_every_cached_node(self) -> None:
        # Renaming an option (Todo to Inbox during setup, or by hand) bumps
        # no item timestamp, but the old name is denormalized into every
        # cached fieldValues node; the fields fingerprint must catch it.
        github = self.github()
        github._fetch_project()
        github.reset_counters()
        github.metadata["fields"]["nodes"][0]["options"][0]["name"] = "Inbox"
        for node in github.items:
            node["fieldValues"]["nodes"][0]["name"] = "Inbox"

        project, _ = github._fetch_project()

        self.assertEqual(1, github.probe_pages)
        self.assertEqual(1, github.full_pages)
        self.assertEqual([], github.hydration_batches)
        self.assertEqual("Inbox", project["items"][0]["fieldValues"]["nodes"][0]["name"])
        cached_node = self.read_cache()["items"]["item-1"]["node"]
        self.assertEqual("Inbox", cached_node["fieldValues"]["nodes"][0]["name"])

    def test_expired_cache_entries_are_rehydrated(self) -> None:
        github = self.github()
        github._fetch_project()
        github.reset_counters()
        cache = self.read_cache()
        expired = iso(datetime.now(UTC) - timedelta(seconds=ITEM_CACHE_MAX_AGE_SECONDS + 60))
        for entry in cache["items"].values():
            entry["fetched_at"] = expired
        self.write_cache(cache)

        github._fetch_project()

        self.assertEqual(0, github.full_pages)
        self.assertEqual([["item-1", "item-2"]], github.hydration_batches)
        for entry in self.read_cache()["items"].values():
            self.assertNotEqual(expired, entry["fetched_at"])

    def test_content_changed_in_the_fetch_second_is_not_trusted(self) -> None:
        # updatedAt has one-second resolution: a second change landing in the
        # same second as the hydration would compare equal forever.
        now = datetime.now(UTC)
        changed = iso(now - timedelta(seconds=1))
        github = self.github(
            [full_item("item-1", 11, item_updated=changed, content_updated=changed)]
        )
        github._fetch_project()
        github.reset_counters()
        cache = self.read_cache()
        cache["items"]["item-1"]["fetched_at"] = iso(now)
        self.write_cache(cache)

        github._fetch_project()

        self.assertEqual([["item-1"]], github.hydration_batches)

    def test_removed_item_disappears_from_project_and_cache(self) -> None:
        github = self.github()
        github._fetch_project()
        github.reset_counters()
        del github.items[0]

        project, _ = github._fetch_project()

        self.assertEqual(["item-2"], [node["id"] for node in project["items"]])
        self.assertEqual([], github.hydration_batches)
        self.assertEqual({"item-2"}, set(self.read_cache()["items"]))

    def test_new_item_is_hydrated_and_joins_the_cache(self) -> None:
        github = self.github()
        github._fetch_project()
        github.reset_counters()
        github.items.append(
            full_item(
                "item-3",
                13,
                item_updated="2026-01-07T00:00:00Z",
                content_updated="2026-01-07T00:00:00Z",
            )
        )

        project, _ = github._fetch_project()

        self.assertEqual([["item-3"]], github.hydration_batches)
        self.assertEqual(["item-1", "item-2", "item-3"], [node["id"] for node in project["items"]])
        self.assertIn("item-3", self.read_cache()["items"])

    def test_reused_entries_keep_their_original_fetched_at(self) -> None:
        # fetched_at records when a node truly came from GitHub; refreshing
        # it on reuse would let an edited comment dodge the expiry forever.
        github = self.github()
        github._fetch_project()
        original = self.read_cache()["items"]["item-1"]["fetched_at"]
        github.reset_counters()
        github.items[1]["updatedAt"] = "2026-01-08T00:00:00Z"

        github._fetch_project()

        self.assertEqual([["item-2"]], github.hydration_batches)
        self.assertEqual(original, self.read_cache()["items"]["item-1"]["fetched_at"])

    def test_corrupt_cache_falls_back_to_full_fetch_and_is_rewritten(self) -> None:
        github = self.github()
        self.cache_path.write_text("not json{", encoding="utf-8")

        github._fetch_project()

        self.assertEqual(1, github.full_pages)
        self.assertEqual(0, github.probe_pages)
        self.assertEqual(ITEM_CACHE_SCHEMA_VERSION, self.read_cache()["schema_version"])

    def test_version_mismatched_cache_falls_back_to_full_fetch(self) -> None:
        github = self.github()
        github._fetch_project()
        cache = self.read_cache()
        cache["schema_version"] = ITEM_CACHE_SCHEMA_VERSION + 1
        self.write_cache(cache)
        github.reset_counters()

        github._fetch_project()

        self.assertEqual(1, github.full_pages)
        self.assertEqual(0, github.probe_pages)

    def test_cache_for_a_different_project_is_not_trusted(self) -> None:
        github = self.github()
        github._fetch_project()
        cache = self.read_cache()
        cache["project"] = "acme/9"
        self.write_cache(cache)
        github.reset_counters()

        github._fetch_project()

        self.assertEqual(1, github.full_pages)
        self.assertEqual(0, github.probe_pages)

    def test_item_deleted_between_probe_and_hydration_falls_back(self) -> None:
        github = self.github()
        github._fetch_project()
        github.reset_counters()
        github.items[0]["updatedAt"] = "2026-01-08T00:00:00Z"
        github.deleted_hydration_ids.add("item-1")

        project, _ = github._fetch_project()

        # The hydration error abandons the incremental attempt; the full
        # fetch still returns the complete, correct board.
        self.assertEqual(1, github.full_pages)
        self.assertEqual(["item-1", "item-2"], [node["id"] for node in project["items"]])

    def test_rate_limited_hydration_propagates_for_serve_stale(self) -> None:
        github = self.github()
        github._fetch_project()
        github.reset_counters()
        github.items[0]["updatedAt"] = "2026-01-08T00:00:00Z"
        github.hydration_error = RateLimitError("API rate limit exceeded")

        with self.assertRaises(RateLimitError):
            github._fetch_project()

        self.assertEqual(0, github.full_pages)

    def test_incremental_result_matches_a_full_fetch_of_the_same_board(self) -> None:
        github = self.github()
        github._fetch_project()
        github.items[0]["content"]["updatedAt"] = "2026-01-09T00:00:00Z"
        github.items[0]["content"]["title"] = "Renamed card"
        incremental_project, _ = github._fetch_project()

        control = self.github(copy.deepcopy(github.items))
        control.cache_path = Path(self._tmp.name) / "control.json"
        full_project, _ = control._fetch_project_full()

        self.assertEqual(full_project, incremental_project)

    def test_multi_page_probe_covers_the_whole_board(self) -> None:
        github = self.github()
        github.page_size = 1
        github._fetch_project()
        github.reset_counters()

        project, _ = github._fetch_project()

        self.assertEqual(0, github.full_pages)
        self.assertEqual(2, github.probe_pages)
        self.assertEqual([], github.hydration_batches)
        self.assertEqual(["item-1", "item-2"], [node["id"] for node in project["items"]])

    def test_draft_items_are_always_rehydrated(self) -> None:
        # The probe cannot see a draft's content timestamp, so a cached draft
        # is never trusted; correctness beats savings for the odd draft card.
        github = self.github([draft_item("item-d", item_updated="2026-01-02T00:00:00Z")])
        github._fetch_project()
        github.reset_counters()

        project, _ = github._fetch_project()

        self.assertEqual([["item-d"]], github.hydration_batches)
        self.assertEqual(["item-d"], [node["id"] for node in project["items"]])

    def test_full_refresh_environment_variable_forces_the_full_fetch(self) -> None:
        github = self.github()
        github._fetch_project()
        github.reset_counters()

        with mock.patch.dict(os.environ, {"KANBANLAN_FULL_REFRESH": "1"}):
            github._fetch_project()

        self.assertEqual(1, github.full_pages)
        self.assertEqual(0, github.probe_pages)

    def test_changed_items_hydrate_in_batches_of_thirty(self) -> None:
        items = [
            full_item(
                f"item-{index}",
                index,
                item_updated="2026-01-02T00:00:00Z",
                content_updated="2026-01-02T00:00:00Z",
            )
            for index in range(1, 36)
        ]
        github = self.github(items)
        github._fetch_project()
        github.reset_counters()
        for node in github.items:
            node["content"]["updatedAt"] = "2026-01-10T00:00:00Z"

        github._fetch_project()

        self.assertEqual([30, 5], [len(batch) for batch in github.hydration_batches])

    def test_unwritable_cache_path_never_fails_the_fetch(self) -> None:
        github = self.github()
        github.cache_path = Path(self._tmp.name) / "blocked" / "cache.json"
        # A file where the parent directory should be makes every write fail.
        github.cache_path.parent.write_text("in the way", encoding="utf-8")

        project, _ = github._fetch_project()

        self.assertEqual(["item-1", "item-2"], [node["id"] for node in project["items"]])
