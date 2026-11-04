from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config
from kanbanlan.github import (
    ITEM_CACHE_SCHEMA_VERSION,
    GitHub,
    closing_references,
    queue_items,
)
from kanbanlan.rest import ConditionalReader, api_path
from kanbanlan.runner import CommandError, CommandResult, RateLimitError
from kanbanlan.snapshot import (
    READ_FROM_ISSUES,
    READ_FROM_PROJECT,
    SCHEMA_VERSION,
    CacheStore,
    isoformat,
    read_from,
)

REPOSITORY = "acme/widget"
IDENTITY = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER_IDENTITY = "KBL-BBBBBBBBBBBBBBBBBBBBBBBBBB"


def config() -> Config:
    return Config(
        repository=REPOSITORY,
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
        rate_limit_floor=200,
    )


PROJECT_METADATA = {
    "id": "PVT_1",
    "number": 2,
    "title": "Delivery",
    "url": "https://github.com/orgs/acme/projects/2",
    "updatedAt": "2026-01-05T00:00:00Z",
    "repositories": {"pageInfo": {"hasNextPage": False}, "nodes": [{"nameWithOwner": REPOSITORY}]},
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


def rest_issue(number: int, *, labels=(), state="open", body=None, comments=0, **extra):
    return {
        "node_id": f"I_{number}",
        "number": number,
        "title": f"Request {number}",
        "body": body if body is not None else f"<!-- kanbanlan:id={IDENTITY} -->",
        "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "state": state,
        "state_reason": "completed" if state == "closed" else None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": "2026-01-03T00:00:00Z" if state == "closed" else None,
        "labels": [{"name": name, "color": "ededed"} for name in labels],
        "milestone": None,
        "assignees": [],
        "comments": comments,
        **extra,
    }


def rest_comment(number: int, body: str, created_at: str = "2026-01-02T00:00:00Z"):
    return {
        "issue_url": f"https://api.github.com/repos/{REPOSITORY}/issues/{number}",
        "body": body,
        "created_at": created_at,
        "user": {"login": "monalisa"},
    }


def rest_pull(number: int, body: str, *, repository: str = REPOSITORY):
    return {
        "number": number,
        "title": f"Deliver {number}",
        "body": body,
        "html_url": f"https://github.com/{repository}/pull/{number}",
        "draft": False,
        "created_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "user": {"login": "monalisa"},
        "labels": [],
        "head": {"ref": "work/x"},
        "base": {"ref": "main", "repo": {"full_name": repository}},
    }


ISSUES_PATH = f"repos/{REPOSITORY}/issues?state=all&per_page=100&sort=created&direction=asc"
ISSUES_PAGE_2 = "repositories/1/issues?state=all&per_page=100&after=abc"
COMMENTS_PATH = f"repos/{REPOSITORY}/issues/comments?per_page=100&sort=created&direction=asc"
PULLS_PATH = f"repos/{REPOSITORY}/pulls?state=open&per_page=100&sort=created&direction=asc"


class FakeGh:
    """Answer ``gh api --include`` the way GitHub does, ETags and 304s included."""

    def __init__(self, pages: dict[str, tuple[list, str | None]]):
        self.pages = pages
        self.versions: dict[str, int] = {path: 1 for path in pages}
        self.calls: list[list[str]] = []
        self.graphql_calls = 0
        self.remaining = 4999

    def change(self, path: str, body: list, next_path: str | None = None) -> None:
        self.pages[path] = (body, next_path)
        self.versions[path] = self.versions.get(path, 0) + 1

    def etag(self, path: str) -> str:
        return f'W/"{path}@{self.versions[path]}"'

    def run(self, args, **_options):
        self.calls.append(list(args))
        if args[:3] == ["gh", "api", "graphql"]:
            self.graphql_calls += 1
            payload = {"data": {"organization": {"projectV2": PROJECT_METADATA}}}
            return CommandResult(tuple(args), 0, json.dumps(payload), "")
        assert args[:3] == ["gh", "api", "--include"], args
        path = args[-1]
        sent = None
        if "-H" in args:
            sent = args[args.index("-H") + 1].removeprefix("If-None-Match: ")
        if path not in self.pages:
            return CommandResult(
                tuple(args), 1, "HTTP/2.0 404 Not Found\n\n{}", "gh: Not Found (HTTP 404)"
            )
        body, next_path = self.pages[path]
        headers = [
            f"Etag: {self.etag(path)}",
            f"X-Ratelimit-Remaining: {self.remaining}",
            "X-Ratelimit-Limit: 5000",
            "X-Ratelimit-Reset: 1893456000",
        ]
        if next_path:
            headers.append(f'Link: <https://api.github.com/{next_path}>; rel="next"')
        if sent == self.etag(path):
            head = "HTTP/2.0 304 Not Modified\n" + "\n".join(headers)
            return CommandResult(tuple(args), 1, head + "\n\n", "gh: HTTP 304")
        self.remaining -= 1
        head = "HTTP/2.0 200 OK\n" + "\n".join(headers)
        return CommandResult(tuple(args), 0, f"{head}\n\n{json.dumps(body)}", "")

    def rest_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[:3] == ["gh", "api", "--include"]]


def default_pages() -> dict[str, tuple[list, str | None]]:
    return {
        ISSUES_PATH: (
            [
                rest_issue(1, labels=["status:ready", "priority:p1"]),
                rest_issue(2, labels=["status:in-progress"], comments=1),
            ],
            ISSUES_PAGE_2,
        ),
        ISSUES_PAGE_2: (
            [
                rest_issue(3, state="closed"),
                rest_issue(4, body="no identity, no status"),
                {**rest_issue(5, labels=["status:review"]), "pull_request": {"url": "x"}},
                rest_issue(6, labels=["status:ready"], body=f"kanbanlan:id={OTHER_IDENTITY}"),
            ],
            None,
        ),
        COMMENTS_PATH: (
            [
                rest_comment(
                    2,
                    "CLAIM: 2026-01-02T00:00:00Z\nSession: wf-1\nTouchpoints: github.py",
                )
            ],
            None,
        ),
        PULLS_PATH: ([rest_pull(9, "Closes #6\n\n`Closes #1`")], None),
    }


class QueueSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        patcher = mock.patch("kanbanlan.github.cache_dir", return_value=self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = FakeGh(default_pages())
        self.github = GitHub(Path(self.directory.name), config(), runner=self.gh)

    def read(self) -> dict:
        return self.github.queue_snapshot(generated_at=datetime.now(UTC))

    def test_queue_is_read_from_labels_without_the_project(self) -> None:
        snapshot = self.read()

        self.assertEqual(READ_FROM_ISSUES, read_from(snapshot))
        self.assertEqual(SCHEMA_VERSION, snapshot["schema_version"])
        by_number = {item["number"]: item for item in snapshot["items"]}
        # Pull requests and issues that are not requests are left out.
        self.assertEqual([1, 2, 3, 6], sorted(by_number))
        self.assertEqual("Ready", by_number[1]["status"])
        self.assertEqual("In progress", by_number[2]["status"])
        self.assertEqual("Done", by_number[3]["status"])
        self.assertEqual("wf-1", by_number[2]["active_claim"]["session"])
        self.assertEqual("I_1", by_number[1]["provider_id"])
        # A closing keyword links the pull request; one inside code does not.
        self.assertEqual([9], [pr["number"] for pr in by_number[6]["linked_open_pull_requests"]])
        self.assertEqual([], by_number[1]["linked_open_pull_requests"])
        # Queue order: priority first, so #1 (p1) beats #6 (unprioritized).
        self.assertEqual(1, snapshot["next_ready"]["number"])
        self.assertEqual("field-status", snapshot["project"]["fields"]["nodes"][0]["id"])
        self.assertEqual("core", snapshot["rate_limit"]["resource"])
        self.assertEqual(0, snapshot["rate_limit"]["cost"])

    def test_snapshot_keys_match_the_project_read(self) -> None:
        snapshot = self.read()
        project_item = {
            "id": "PVTI_1",
            "type": "ISSUE",
            "isArchived": False,
            "fieldValues": {"nodes": []},
            "content": {
                "id": "I_1",
                "number": 1,
                "title": "t",
                "body": "",
                "url": "u",
                "state": "OPEN",
                "labels": {"nodes": []},
                "comments": {"totalCount": 0, "nodes": []},
                "repository": {"nameWithOwner": REPOSITORY},
            },
        }
        from kanbanlan.snapshot import build_snapshot

        reference = build_snapshot(config(), {"items": [project_item]}, [], {})
        self.assertEqual(set(reference), set(snapshot))
        self.assertEqual(set(reference["items"][0]), set(snapshot["items"][0]))
        self.assertEqual(set(reference["source"]), set(snapshot["source"]))

    def test_unchanged_queue_revalidates_with_304s_and_no_graphql(self) -> None:
        first = self.read()
        self.assertEqual(1, self.gh.graphql_calls)  # Project metadata, once
        self.gh.calls.clear()

        second = self.read()

        rest = self.gh.rest_calls()
        self.assertEqual(4, len(rest))
        self.assertTrue(all("-H" in call for call in rest), rest)
        self.assertEqual(1, self.gh.graphql_calls)
        self.assertEqual(4, second["rate_limit"]["not_modified"])
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(first["open_pull_requests"], second["open_pull_requests"])

    def test_a_changed_page_is_read_again_and_others_stay_304(self) -> None:
        self.read()
        body, next_path = self.gh.pages[ISSUES_PAGE_2]
        self.gh.change(ISSUES_PAGE_2, [*body[:1], rest_issue(7, labels=["status:ready"])])

        snapshot = self.read()

        self.assertEqual(3, snapshot["rate_limit"]["not_modified"])
        self.assertIn(7, [item["number"] for item in snapshot["items"]])
        self.assertNotIn(6, [item["number"] for item in snapshot["items"]])

    def test_a_new_page_is_followed_past_the_cached_chain(self) -> None:
        self.read()
        page_3 = "repositories/1/issues?state=all&per_page=100&after=def"
        body, _ = self.gh.pages[ISSUES_PAGE_2]
        self.gh.change(ISSUES_PAGE_2, body, page_3)
        self.gh.change(page_3, [rest_issue(8, labels=["status:intake"])])

        snapshot = self.read()

        self.assertIn(8, [item["number"] for item in snapshot["items"]])

    def test_project_item_ids_come_from_the_last_project_read(self) -> None:
        entry = {"node": {"id": "PVTI_1", "isArchived": False, "content": {"id": "I_1"}}}
        (self.cache / "project_items.json").write_text(
            json.dumps(
                {
                    "schema_version": ITEM_CACHE_SCHEMA_VERSION,
                    "project": "acme/2",
                    "fields_fingerprint": "x",
                    "repositories": [REPOSITORY],
                    "items": {"PVTI_1": entry},
                }
            )
        )

        snapshot = self.read()

        by_number = {item["number"]: item for item in snapshot["items"]}
        self.assertEqual("PVTI_1", by_number[1]["project_item_id"])
        self.assertIsNone(by_number[2]["project_item_id"])

    def test_peer_repository_pull_requests_are_read_and_failures_reported(self) -> None:
        metadata = {
            **PROJECT_METADATA,
            "repositories": {
                "nodes": [{"nameWithOwner": REPOSITORY}, {"nameWithOwner": "acme/peer"}]
            },
        }
        peer = "repos/acme/peer/pulls?state=open&per_page=100&sort=created&direction=asc"
        self.gh.pages[peer] = (
            [rest_pull(3, f"Closes {REPOSITORY}#1", repository="acme/peer")],
            None,
        )
        self.gh.versions[peer] = 1
        with mock.patch.object(GitHub, "projection_metadata", return_value=metadata):
            snapshot = self.read()

        by_number = {item["number"]: item for item in snapshot["items"]}
        linked = by_number[1]["linked_open_pull_requests"]
        self.assertEqual(["acme/peer"], [pr["repository"] for pr in linked])

        del self.gh.pages[peer]
        snapshot = self.read()
        self.assertEqual(
            ["acme/peer"],
            [value["repository"] for value in snapshot["source"]["unavailable_repositories"]],
        )

    def test_quota_refusal_raises_rate_limit_error(self) -> None:
        original = self.gh.run

        def refuse(args, **options):
            if args[:3] == ["gh", "api", "--include"]:
                head = (
                    "HTTP/2.0 403 Forbidden\nX-Ratelimit-Remaining: 0\n"
                    "X-Ratelimit-Reset: 1893456000"
                )
                return CommandResult(
                    tuple(args),
                    1,
                    head + '\n\n{"message": "API rate limit exceeded"}',
                    "gh: API rate limit exceeded (HTTP 403)",
                )
            return original(args, **options)

        self.gh.run = refuse
        with self.assertRaises(RateLimitError) as caught:
            self.read()
        self.assertEqual("2030-01-01T00:00:00Z", caught.exception.reset_at)

    def test_metadata_is_renewed_only_when_old_and_survives_refusal(self) -> None:
        self.read()
        path = self.cache / "project_metadata.json"
        data = json.loads(path.read_text())
        data["fetched_at"] = isoformat(datetime.now(UTC) - timedelta(days=1))
        path.write_text(json.dumps(data))
        with mock.patch.object(
            GitHub, "projection_metadata", side_effect=RateLimitError("out")
        ) as metadata:
            snapshot = self.read()
        metadata.assert_called_once()
        self.assertEqual("PVT_1", snapshot["project"]["id"])


class ConditionalReaderTests(unittest.TestCase):
    def test_unknown_304_refetches_without_an_etag(self) -> None:
        gh = FakeGh({PULLS_PATH: ([rest_pull(1, "")], None)})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rest_cache.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": {PULLS_PATH: {"etag": gh.etag(PULLS_PATH)}},
                    }
                )
            )
            reader = ConditionalReader(gh, path)

            items = reader.collection(PULLS_PATH)

        self.assertEqual([1], [value["number"] for value in items])
        self.assertEqual(2, len(gh.rest_calls()))

    def test_other_failures_raise_command_error(self) -> None:
        gh = FakeGh({})
        reader = ConditionalReader(gh, None)
        with self.assertRaises(CommandError):
            reader.collection(PULLS_PATH)

    def test_corrupt_cache_is_ignored(self) -> None:
        gh = FakeGh({PULLS_PATH: ([], None)})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rest_cache.json"
            path.write_text("{not json")
            reader = ConditionalReader(gh, path)
            self.assertEqual([], reader.collection(PULLS_PATH))
            reader.save()
            self.assertIn(PULLS_PATH, json.loads(path.read_text())["entries"])

    def test_api_path_strips_host_and_enterprise_prefix(self) -> None:
        self.assertEqual(
            "repositories/1/issues?page=2",
            api_path("https://api.github.com/repositories/1/issues?page=2"),
        )
        self.assertEqual(
            "repositories/1/issues",
            api_path("https://ghe.example.com/api/v3/repositories/1/issues"),
        )


class ClosingReferenceTests(unittest.TestCase):
    def test_keywords_and_qualified_references(self) -> None:
        body = (
            "Fixes #3, resolves: acme/peer#4\n"
            "Closes https://github.com/acme/other/issues/5\n"
            "Mentions #6 without a keyword\n"
            "```\nCloses #7\n```\n<!-- closes #8 -->"
        )
        self.assertEqual(
            [(REPOSITORY, 3), ("acme/peer", 4), ("acme/other", 5)],
            closing_references(body, REPOSITORY),
        )

    def test_queue_items_keep_the_last_comment_window(self) -> None:
        comments = [
            rest_comment(1, f"note {index}", f"2026-01-02T00:{index // 60:02d}:{index % 60:02d}Z")
            for index in range(105)
        ]
        items = queue_items(REPOSITORY, [rest_issue(1, comments=105)], comments, {})
        thread = items[0]["content"]["comments"]
        self.assertEqual(105, thread["totalCount"])
        self.assertEqual(100, len(thread["nodes"]))
        self.assertEqual("note 104", thread["nodes"][-1]["body"])


class CacheStoreRoutingTests(unittest.TestCase):
    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def _snapshot(self, kind: str, generated_at: datetime) -> dict:
            self.calls.append(kind)
            return {
                "schema_version": SCHEMA_VERSION,
                "generated_at": isoformat(generated_at),
                "source": {"read_from": kind},
                "items": [],
                "rate_limit": {
                    "resource": "core",
                    "remaining": 1,
                    "resetAt": "2999-01-01T00:00:00Z",
                },
            }

        def queue_snapshot(self, *, generated_at: datetime) -> dict:
            return self._snapshot(READ_FROM_ISSUES, generated_at)

        def snapshot(self, *, generated_at: datetime) -> dict:
            return self._snapshot(READ_FROM_PROJECT, generated_at)

    def test_queue_reads_by_default_and_project_reads_for_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            client = self.Client()

            store.ensure(client)
            store.refresh(client)
            store.refresh(client, projection=True)

        self.assertEqual([READ_FROM_ISSUES, READ_FROM_ISSUES, READ_FROM_PROJECT], client.calls)

    def test_rest_quota_never_trips_the_graphql_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            client = self.Client()
            snapshot = store.refresh(client)
            self.assertIsNone(store.rate_limit_deferral(snapshot))
            store.invalidate()
            store.ensure(client)
        self.assertEqual([READ_FROM_ISSUES, READ_FROM_ISSUES], client.calls)

    def test_a_queue_reader_is_not_held_by_the_graphql_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            low = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": isoformat(datetime.now(UTC) - timedelta(hours=1)),
                "items": [],
                "rate_limit": {"remaining": 10, "resetAt": "2999-01-01T00:00:00Z"},
            }
            store._write_json(store.snapshot_path, low)
            client = self.Client()

            store.ensure(client)

            self.assertEqual([READ_FROM_ISSUES], client.calls)

    def test_rate_limit_text_names_the_quota_it_reports(self) -> None:
        from kanbanlan.cli import _rate_limit_text

        self.assertEqual(
            "4990 REST requests remaining, 4 of 4 reads unchanged",
            _rate_limit_text(
                {"resource": "core", "remaining": 4990, "requests": 4, "not_modified": 4}
            ),
        )
        self.assertEqual(
            "12 GraphQL points remaining, resets 2030-01-01T00:00:00Z",
            _rate_limit_text({"remaining": 12, "resetAt": "2030-01-01T00:00:00Z"}),
        )

    def test_read_from_defaults_to_the_project_for_older_documents(self) -> None:
        self.assertEqual(READ_FROM_PROJECT, read_from({"source": {}}))
        self.assertEqual(READ_FROM_PROJECT, read_from(None))


if __name__ == "__main__":
    unittest.main()
