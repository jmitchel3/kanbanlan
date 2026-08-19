from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from kanbanlan.cli import _cmd_overlap, _cmd_snapshot, _cmd_status, build_parser
from kanbanlan.config import Config
from kanbanlan.domain import resolve_request_item
from kanbanlan.github import GitHub, project_repositories
from kanbanlan.identity import attach_kanbanlan_id, new_kanbanlan_id
from kanbanlan.runner import CommandError, CommandResult
from kanbanlan.snapshot import SCOPE_PROJECT, SCOPE_REPOSITORY, build_snapshot

LOCAL = "acme/widget"
PEER = "acme/website"
GENERATED_AT = datetime(2026, 8, 19, tzinfo=UTC)


def config() -> Config:
    return Config(
        repository=LOCAL,
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def issue_item(
    number: int,
    *,
    repository: str = LOCAL,
    status: str = "Ready",
    body: str = "Request body",
    state: str = "OPEN",
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"item-{repository}-{number}",
        "type": "ISSUE",
        "isArchived": False,
        "fieldValues": {"nodes": [{"name": status, "field": {"name": "Status"}}]},
        "content": {
            "id": f"issue-{repository}-{number}",
            "number": number,
            "title": f"Request {number} in {repository}",
            "body": body,
            "url": f"https://github.test/{repository}/issues/{number}",
            "state": state,
            "stateReason": None,
            "createdAt": "2026-08-19T00:00:00Z",
            "updatedAt": "2026-08-19T01:00:00Z",
            "closedAt": None,
            "repository": {"nameWithOwner": repository},
            "labels": {"nodes": [{"name": "priority:p1", "color": "000000"}]},
            "assignees": {"nodes": []},
            "comments": {"nodes": comments or []},
        },
    }


def draft_item(identifier: str = "draft-1") -> dict[str, Any]:
    return {
        "id": identifier,
        "type": "DRAFT_ISSUE",
        "isArchived": False,
        "fieldValues": {"nodes": [{"name": "Inbox", "field": {"name": "Status"}}]},
        "content": {"id": identifier, "title": "Draft note", "body": "not a request"},
    }


def pull_request(
    number: int,
    *,
    repository: str = LOCAL,
    body: str = "",
    closes: list[tuple[str, int]] | None = None,
    is_draft: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Pull request {number} in {repository}",
        "body": body,
        "url": f"https://github.test/{repository}/pull/{number}",
        "repository": {"nameWithOwner": repository},
        "headRefName": "work/example",
        "baseRefName": "main",
        "isDraft": is_draft,
        "mergeStateStatus": "CLEAN",
        "createdAt": "2026-08-19T00:00:00Z",
        "updatedAt": "2026-08-19T01:00:00Z",
        "author": {"login": "agent"},
        "labels": {"nodes": []},
        "closingIssuesReferences": {
            "nodes": [
                {
                    "number": issue_number,
                    "url": f"https://github.test/{issue_repository}/issues/{issue_number}",
                    "repository": {"nameWithOwner": issue_repository},
                }
                for issue_repository, issue_number in (closes or [])
            ]
        },
    }


def project(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "project-1",
        "number": 2,
        "title": "Delivery",
        "url": "https://github.test/orgs/acme/projects/2",
        "updatedAt": "2026-08-19T01:00:00Z",
        "fields": {"nodes": []},
        "items": items,
    }


class ProjectScopeSnapshotTests(unittest.TestCase):
    def test_repository_scope_drops_peer_content_and_project_scope_keeps_it(self) -> None:
        value = project([issue_item(7), issue_item(7, repository=PEER), draft_item()])

        repository_scope = build_snapshot(config(), value, [], {}, GENERATED_AT)
        project_scope = build_snapshot(config(), value, [], {}, GENERATED_AT, scope=SCOPE_PROJECT)

        self.assertEqual([LOCAL], [item["repository"] for item in repository_scope["items"]])
        self.assertEqual(
            sorted([LOCAL, PEER]),
            sorted(item["repository"] for item in project_scope["items"]),
        )
        self.assertEqual(SCOPE_REPOSITORY, repository_scope["source"]["scope"])
        self.assertEqual(SCOPE_PROJECT, project_scope["source"]["scope"])
        self.assertEqual(sorted([LOCAL, PEER]), project_scope["source"]["repositories"])

    def test_draft_items_are_skipped_in_both_scopes(self) -> None:
        value = project([draft_item()])

        for scope in (SCOPE_REPOSITORY, SCOPE_PROJECT):
            with self.subTest(scope=scope):
                snapshot = build_snapshot(config(), value, [], {}, GENERATED_AT, scope=scope)
                self.assertEqual([], snapshot["items"])

    def test_same_number_issues_from_different_repositories_never_collide(self) -> None:
        value = project([issue_item(7), issue_item(7, repository=PEER)])

        snapshot = build_snapshot(config(), value, [], {}, GENERATED_AT, scope=SCOPE_PROJECT)

        by_repository = {item["repository"]: item for item in snapshot["items"]}
        self.assertEqual(f"github:{LOCAL}#7", by_repository[LOCAL]["provider_ref"])
        self.assertEqual(f"github:{PEER}#7", by_repository[PEER]["provider_ref"])
        self.assertEqual("#7", by_repository[LOCAL]["display_id"])
        self.assertEqual(f"{PEER}#7", by_repository[PEER]["display_id"])

    def test_bare_number_resolves_to_the_configured_repository_only(self) -> None:
        value = project([issue_item(7), issue_item(7, repository=PEER)])
        snapshot = build_snapshot(config(), value, [], {}, GENERATED_AT, scope=SCOPE_PROJECT)

        self.assertEqual(LOCAL, resolve_request_item(snapshot, "7")["repository"])
        self.assertEqual(LOCAL, resolve_request_item(snapshot, "#7")["repository"])
        self.assertEqual(PEER, resolve_request_item(snapshot, f"{PEER}#7")["repository"])
        self.assertEqual(PEER, resolve_request_item(snapshot, f"github:{PEER}#7")["repository"])

    def test_same_number_pull_requests_from_different_repositories_never_collide(self) -> None:
        value = project([issue_item(7), issue_item(7, repository=PEER)])
        pull_requests = [
            pull_request(11, closes=[(LOCAL, 7)]),
            pull_request(11, repository=PEER, closes=[(PEER, 7)]),
        ]

        snapshot = build_snapshot(
            config(), value, pull_requests, {}, GENERATED_AT, scope=SCOPE_PROJECT
        )

        by_repository = {item["repository"]: item for item in snapshot["items"]}
        for repository, item in by_repository.items():
            with self.subTest(repository=repository):
                linked = item["linked_open_pull_requests"]
                self.assertEqual([repository], [value["repository"] for value in linked])
                self.assertEqual(f"github:{repository}#11", linked[0]["provider_ref"])

    def test_project_scope_never_hands_this_repository_peer_work(self) -> None:
        value = project(
            [
                issue_item(7, repository=PEER, status="Ready"),
                issue_item(9, status="Ready"),
            ]
        )

        snapshot = build_snapshot(config(), value, [], {}, GENERATED_AT, scope=SCOPE_PROJECT)

        self.assertEqual([9], [item["number"] for item in snapshot["ready_cards"]])
        self.assertEqual(LOCAL, snapshot["next_ready"]["repository"])

    def test_peer_claims_and_touchpoints_stay_visible_under_project_scope(self) -> None:
        comment = {
            "body": "CLAIM: 2026-08-19T00:00:00Z\nSession: peer-1\nTouchpoints: src/site",
            "createdAt": "2026-08-19T00:00:01Z",
            "author": {"login": "peer-agent"},
        }
        value = project([issue_item(7, repository=PEER, comments=[comment])])

        snapshot = build_snapshot(config(), value, [], {}, GENERATED_AT, scope=SCOPE_PROJECT)

        claim = snapshot["items"][0]["active_claim"]
        self.assertEqual("peer-1", claim["session"])
        self.assertEqual("src/site", claim["touchpoints"])

    def test_unavailable_peer_repositories_are_reported_in_the_snapshot(self) -> None:
        snapshot = build_snapshot(
            config(),
            project([issue_item(7)]),
            [],
            {},
            GENERATED_AT,
            scope=SCOPE_PROJECT,
            unavailable_repositories=[{"repository": PEER, "error": "not found"}],
        )

        self.assertEqual(
            [{"repository": PEER, "error": "not found"}],
            snapshot["source"]["unavailable_repositories"],
        )

    def test_unsupported_scope_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_snapshot(config(), project([]), [], {}, GENERATED_AT, scope="owner")


class FakeGitHub(GitHub):
    """A GitHub provider whose GraphQL reads are scripted, not networked."""

    def __init__(
        self,
        *,
        project_pages: list[dict[str, Any]],
        pull_request_pages: dict[str, list[dict[str, Any]]],
        unreadable: dict[str, str] | None = None,
    ):
        super().__init__(Path("/tmp"), config())
        self.project_pages = project_pages
        self.pull_request_pages = pull_request_pages
        self.unreadable = unreadable or {}
        self.project_cursors: list[str | None] = []
        self.pull_request_calls: list[tuple[str, str | None]] = []

    def graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        retry: bool = False,
    ) -> dict[str, Any]:
        if "projectV2" in query:
            self.project_cursors.append(variables["after"])
            return self.project_pages[len(self.project_cursors) - 1]
        repository = f"{variables['owner']}/{variables['repo']}"
        self.pull_request_calls.append((repository, variables["after"]))
        if repository in self.unreadable:
            raise CommandError(
                CommandResult(("gh", "api", "graphql"), 1, "", self.unreadable[repository])
            )
        pages = self.pull_request_pages.get(repository, [])
        index = sum(1 for value, _ in self.pull_request_calls if value == repository) - 1
        if index >= len(pages):
            return {"repository": None, "rateLimit": {}}
        return pages[index]


def project_page(
    items: list[dict[str, Any]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    remaining: int = 5000,
) -> dict[str, Any]:
    value = project(items)
    value.pop("items")
    value["items"] = {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": items,
    }
    return {
        "organization": {"projectV2": value},
        "rateLimit": {"cost": 1, "remaining": remaining, "resetAt": "2026-08-19T02:00:00Z"},
    }


def pull_request_page(
    values: list[dict[str, Any]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    remaining: int = 5000,
) -> dict[str, Any]:
    return {
        "repository": {
            "pullRequests": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": values,
            }
        },
        "rateLimit": {"cost": 1, "remaining": remaining, "resetAt": "2026-08-19T02:00:00Z"},
    }


class ProjectScopeCollectionTests(unittest.TestCase):
    def test_project_repositories_are_derived_from_project_content(self) -> None:
        value = project([issue_item(7), issue_item(8, repository=PEER), draft_item()])

        self.assertEqual({LOCAL, PEER}, project_repositories(value))

    def test_repository_scope_reads_only_the_configured_repository(self) -> None:
        github = FakeGitHub(
            project_pages=[project_page([issue_item(7), issue_item(8, repository=PEER)])],
            pull_request_pages={
                LOCAL: [pull_request_page([pull_request(11)])],
                PEER: [pull_request_page([pull_request(12, repository=PEER)])],
            },
        )

        read = github.collect()

        self.assertEqual([LOCAL], [repository for repository, _ in github.pull_request_calls])
        self.assertEqual([11], [value["number"] for value in read.pull_requests])

    def test_project_scope_reads_every_project_repository_and_no_others(self) -> None:
        github = FakeGitHub(
            project_pages=[project_page([issue_item(7), issue_item(8, repository=PEER)])],
            pull_request_pages={
                LOCAL: [pull_request_page([pull_request(11)])],
                PEER: [pull_request_page([pull_request(12, repository=PEER)])],
            },
        )

        read = github.collect(scope=SCOPE_PROJECT)

        self.assertEqual(
            sorted([LOCAL, PEER]),
            sorted({repository for repository, _ in github.pull_request_calls}),
        )
        self.assertEqual([11, 12], sorted(value["number"] for value in read.pull_requests))

    def test_project_and_pull_request_pagination_is_followed(self) -> None:
        github = FakeGitHub(
            project_pages=[
                project_page([issue_item(7)], has_next=True, cursor="page-2"),
                project_page([issue_item(8, repository=PEER)]),
            ],
            pull_request_pages={
                LOCAL: [
                    pull_request_page([pull_request(11)], has_next=True, cursor="pr-2"),
                    pull_request_page([pull_request(12)]),
                ],
                PEER: [pull_request_page([pull_request(13, repository=PEER)])],
            },
        )

        read = github.collect(scope=SCOPE_PROJECT)

        self.assertEqual([None, "page-2"], github.project_cursors)
        self.assertIn((LOCAL, "pr-2"), github.pull_request_calls)
        self.assertEqual(
            [7, 8], sorted(value["content"]["number"] for value in read.project["items"])
        )
        self.assertEqual([11, 12, 13], sorted(value["number"] for value in read.pull_requests))

    def test_rate_limit_reports_the_scarcest_remaining_budget(self) -> None:
        github = FakeGitHub(
            project_pages=[
                project_page([issue_item(7), issue_item(8, repository=PEER)], remaining=4000)
            ],
            pull_request_pages={
                LOCAL: [pull_request_page([], remaining=3000)],
                PEER: [pull_request_page([], remaining=1200)],
            },
        )

        read = github.collect(scope=SCOPE_PROJECT)

        self.assertEqual(1200, read.rate_limit["remaining"])

    def test_repository_without_open_pull_requests_is_not_an_error(self) -> None:
        github = FakeGitHub(
            project_pages=[project_page([issue_item(7), issue_item(8, repository=PEER)])],
            pull_request_pages={
                LOCAL: [pull_request_page([])],
                PEER: [pull_request_page([])],
            },
        )

        read = github.collect(scope=SCOPE_PROJECT)

        self.assertEqual([], read.pull_requests)
        self.assertEqual([], read.unavailable_repositories)

    def test_inaccessible_peer_repository_is_reported_rather_than_fatal(self) -> None:
        github = FakeGitHub(
            project_pages=[project_page([issue_item(7), issue_item(8, repository=PEER)])],
            pull_request_pages={LOCAL: [pull_request_page([pull_request(11)])]},
            unreadable={PEER: "Resource not accessible by integration"},
        )

        read = github.collect(scope=SCOPE_PROJECT)

        self.assertEqual([11], [value["number"] for value in read.pull_requests])
        self.assertEqual([PEER], [value["repository"] for value in read.unavailable_repositories])
        self.assertIn("not accessible", read.unavailable_repositories[0]["error"])

    def test_inaccessible_configured_repository_still_fails(self) -> None:
        github = FakeGitHub(
            project_pages=[project_page([issue_item(7)])],
            pull_request_pages={},
            unreadable={LOCAL: "Resource not accessible by integration"},
        )

        with self.assertRaises(CommandError):
            github.collect(scope=SCOPE_PROJECT)

    def test_project_scope_snapshot_is_built_from_every_project_repository(self) -> None:
        kanbanlan_id = new_kanbanlan_id()
        github = FakeGitHub(
            project_pages=[
                project_page(
                    [
                        issue_item(7),
                        issue_item(
                            7,
                            repository=PEER,
                            body=attach_kanbanlan_id("Peer request", kanbanlan_id),
                        ),
                    ]
                )
            ],
            pull_request_pages={
                LOCAL: [pull_request_page([])],
                PEER: [pull_request_page([pull_request(11, repository=PEER, closes=[(PEER, 7)])])],
            },
        )

        snapshot = github.snapshot(generated_at=GENERATED_AT, scope=SCOPE_PROJECT)

        peer = resolve_request_item(snapshot, kanbanlan_id)
        self.assertEqual(PEER, peer["repository"])
        self.assertEqual(f"github:{PEER}#11", peer["linked_open_pull_requests"][0]["provider_ref"])
        self.assertEqual(sorted([LOCAL, PEER]), snapshot["source"]["repositories"])


class ProjectScopeCommandTests(unittest.TestCase):
    """The project scope is read-only and never disturbs the shared cache."""

    def project_snapshot(self) -> dict[str, Any]:
        github = FakeGitHub(
            project_pages=[
                project_page(
                    [
                        issue_item(7, status="In progress"),
                        issue_item(7, repository=PEER, status="Ready"),
                    ]
                )
            ],
            pull_request_pages={
                LOCAL: [pull_request_page([pull_request(11, closes=[(LOCAL, 7)])])],
                PEER: [pull_request_page([pull_request(12, repository=PEER)])],
            },
        )
        return github.snapshot(generated_at=GENERATED_AT, scope=SCOPE_PROJECT)

    def run_command(self, handler: Any, argv: list[str]) -> tuple[int, str, mock.Mock]:
        snapshot = self.project_snapshot()
        provider = mock.Mock()
        provider.provider_name = "github"
        provider.capabilities.project_scope = True
        provider.snapshot.return_value = snapshot
        store = mock.Mock()
        args = build_parser().parse_args(argv)
        stream = StringIO()
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, store),
            ),
            redirect_stdout(stream),
        ):
            code = handler(args)
        return code, stream.getvalue(), store

    def test_project_scope_is_opt_in_and_defaults_to_this_repository(self) -> None:
        self.assertFalse(build_parser().parse_args(["status"]).project_scope)
        self.assertTrue(build_parser().parse_args(["status", "--project"]).project_scope)
        self.assertFalse(build_parser().parse_args(["snapshot"]).project_scope)
        self.assertTrue(build_parser().parse_args(["snapshot", "--project"]).project_scope)

    def test_status_project_reports_every_repository_without_writing_the_cache(self) -> None:
        code, output, store = self.run_command(_cmd_status, ["status", "--project"])

        self.assertEqual(0, code)
        self.assertIn(LOCAL, output)
        self.assertIn(PEER, output)
        self.assertIn("this repository", output)
        store.refresh.assert_not_called()

    def test_snapshot_project_prints_project_scoped_json(self) -> None:
        code, output, store = self.run_command(_cmd_snapshot, ["snapshot", "--project"])
        payload = json.loads(output)

        self.assertEqual(0, code)
        self.assertEqual(SCOPE_PROJECT, payload["source"]["scope"])
        self.assertEqual(sorted([LOCAL, PEER]), payload["source"]["repositories"])
        store.snapshot.assert_not_called()

    def test_overlap_lists_open_cards_and_pull_requests_across_the_project(self) -> None:
        code, output, store = self.run_command(_cmd_overlap, ["overlap"])

        self.assertEqual(0, code)
        self.assertIn(f"github:{LOCAL}#7", output)
        self.assertIn(f"github:{PEER}#7", output)
        self.assertIn(f"github:{LOCAL}#11", output)
        self.assertIn("no linked request", output)
        self.assertIn(f"github:{PEER}#12", output)
        store.refresh.assert_not_called()

    def test_overlap_json_keeps_repository_qualified_references(self) -> None:
        snapshot = self.project_snapshot()
        provider = mock.Mock()
        provider.provider_name = "github"
        provider.capabilities.project_scope = True
        provider.snapshot.return_value = snapshot
        args = build_parser().parse_args(["--json", "overlap"])
        stream = StringIO()
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, mock.Mock()),
            ),
            redirect_stdout(stream),
        ):
            self.assertEqual(0, _cmd_overlap(args))
        payload = json.loads(stream.getvalue())["result"]

        references = sorted(value["provider_ref"] for value in payload["open_requests"])
        self.assertEqual(sorted([f"github:{LOCAL}#7", f"github:{PEER}#7"]), references)
        self.assertEqual(
            [f"github:{PEER}#12"],
            [value["provider_ref"] for value in payload["unlinked_open_pull_requests"]],
        )

    def test_project_scope_is_refused_when_the_canonical_home_cannot_support_it(self) -> None:
        provider = mock.Mock()
        provider.provider_name = "notion"
        provider.capabilities.project_scope = False
        args = build_parser().parse_args(["overlap"])
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, mock.Mock()),
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            _cmd_overlap(args)

        self.assertIn("project scope", str(raised.exception))

    def test_unreadable_peer_repositories_are_surfaced_to_the_operator(self) -> None:
        github = FakeGitHub(
            project_pages=[project_page([issue_item(7), issue_item(8, repository=PEER)])],
            pull_request_pages={LOCAL: [pull_request_page([])]},
            unreadable={PEER: "Resource not accessible by integration"},
        )
        provider = mock.Mock()
        provider.provider_name = "github"
        provider.capabilities.project_scope = True
        provider.snapshot.return_value = github.snapshot(
            generated_at=GENERATED_AT, scope=SCOPE_PROJECT
        )
        args = build_parser().parse_args(["overlap"])
        stream = StringIO()
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, mock.Mock()),
            ),
            redirect_stdout(stream),
        ):
            self.assertEqual(0, _cmd_overlap(args))

        self.assertIn("could not be read", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
