from __future__ import annotations

import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from kanbanlan.cli import _cmd_review
from kanbanlan.config import Config
from kanbanlan.domain import resolve_request_item
from kanbanlan.identity import attach_kanbanlan_id
from kanbanlan.snapshot import SCOPE_PROJECT, build_snapshot
from kanbanlan.workflow import expected_state, plan_reconciliation

LOCAL = "acme/widget"
PEER = "acme/website"
IDENTITY = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER_IDENTITY = "KBL-BBBBBBBBBBBBBBBBBBBBBBBBBB"
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
    kanbanlan_id: str | None = IDENTITY,
    status: str = "In progress",
    state: str = "OPEN",
    labels: list[str] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body = "Request body"
    if kanbanlan_id:
        body = attach_kanbanlan_id(body, kanbanlan_id)
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
            "labels": {
                "nodes": [
                    {"name": name, "color": "000000"}
                    for name in (labels or ["priority:p1", "status:in-progress"])
                ]
            },
            "assignees": {"nodes": []},
            "comments": {"nodes": comments or []},
        },
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


def snapshot(
    items: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]],
    *,
    scope: str | None = None,
) -> dict[str, Any]:
    kwargs = {"scope": scope} if scope else {}
    return build_snapshot(config(), project(items), pull_requests, {}, GENERATED_AT, **kwargs)


def linked_refs(item: dict[str, Any]) -> list[str]:
    return [value["provider_ref"] for value in item["linked_open_pull_requests"]]


class SameRepositoryLinkTests(unittest.TestCase):
    def test_pull_request_and_request_in_the_same_repository_still_link(self) -> None:
        value = snapshot([issue_item(7)], [pull_request(11, closes=[(LOCAL, 7)])])
        item = resolve_request_item(value, IDENTITY)

        self.assertEqual([f"github:{LOCAL}#11"], linked_refs(item))
        self.assertEqual(["closing_reference"], item["linked_open_pull_requests"][0]["linked_by"])

    def test_a_bare_closing_reference_is_read_in_the_pull_request_repository(self) -> None:
        """GitHub resolves a bare ``Closes #7`` inside the pull request's repository.

        A peer pull request written that way therefore arrives with a closing
        reference to its own issue 7, and must not reach this repository's
        issue 7.
        """

        value = snapshot(
            [issue_item(7), issue_item(7, repository=PEER, kanbanlan_id=OTHER_IDENTITY)],
            [pull_request(11, repository=PEER, closes=[(PEER, 7)])],
            scope=SCOPE_PROJECT,
        )

        self.assertEqual([], linked_refs(resolve_request_item(value, IDENTITY)))
        self.assertEqual(
            [f"github:{PEER}#11"], linked_refs(resolve_request_item(value, OTHER_IDENTITY))
        )


class CrossRepositoryLinkTests(unittest.TestCase):
    def test_a_qualified_closing_reference_crosses_the_repository_boundary(self) -> None:
        value = snapshot([issue_item(7)], [pull_request(11, repository=PEER, closes=[(LOCAL, 7)])])
        item = resolve_request_item(value, IDENTITY)

        self.assertEqual([f"github:{PEER}#11"], linked_refs(item))
        self.assertEqual(PEER, item["linked_open_pull_requests"][0]["repository"])

    def test_a_declared_kanbanlan_id_crosses_the_repository_boundary(self) -> None:
        value = snapshot(
            [issue_item(7)],
            [pull_request(11, repository=PEER, body=f"Kanbanlan: `{IDENTITY}`")],
        )
        item = resolve_request_item(value, IDENTITY)

        self.assertEqual([f"github:{PEER}#11"], linked_refs(item))
        self.assertEqual(["kanbanlan_id"], item["linked_open_pull_requests"][0]["linked_by"])

    def test_both_routes_are_reported_when_a_pull_request_uses_each(self) -> None:
        value = snapshot(
            [issue_item(7)],
            [
                pull_request(
                    11,
                    repository=PEER,
                    body=f"Kanbanlan: `{IDENTITY}`",
                    closes=[(LOCAL, 7)],
                )
            ],
        )
        item = resolve_request_item(value, IDENTITY)

        self.assertEqual(
            ["closing_reference", "kanbanlan_id"],
            item["linked_open_pull_requests"][0]["linked_by"],
        )

    def test_identical_issue_numbers_across_repositories_never_collide(self) -> None:
        value = snapshot(
            [issue_item(7), issue_item(7, repository=PEER, kanbanlan_id=OTHER_IDENTITY)],
            [
                pull_request(11, repository=PEER, closes=[(LOCAL, 7)]),
                pull_request(12, closes=[(PEER, 7)]),
            ],
            scope=SCOPE_PROJECT,
        )

        self.assertEqual([f"github:{PEER}#11"], linked_refs(resolve_request_item(value, IDENTITY)))
        self.assertEqual(
            [f"github:{LOCAL}#12"],
            linked_refs(resolve_request_item(value, OTHER_IDENTITY)),
        )

    def test_identical_pull_request_numbers_across_repositories_never_collide(self) -> None:
        value = snapshot(
            [issue_item(7), issue_item(8, repository=PEER, kanbanlan_id=OTHER_IDENTITY)],
            [
                pull_request(11, closes=[(LOCAL, 7)]),
                pull_request(11, repository=PEER, closes=[(PEER, 8)]),
            ],
            scope=SCOPE_PROJECT,
        )

        self.assertEqual([f"github:{LOCAL}#11"], linked_refs(resolve_request_item(value, IDENTITY)))
        self.assertEqual(
            [f"github:{PEER}#11"],
            linked_refs(resolve_request_item(value, OTHER_IDENTITY)),
        )

    def test_unrelated_project_content_is_never_associated(self) -> None:
        value = snapshot(
            [issue_item(7)],
            [
                pull_request(11, repository=PEER, body="Unrelated peer work"),
                pull_request(12, repository=PEER, closes=[(PEER, 99)]),
            ],
        )

        self.assertEqual([], linked_refs(resolve_request_item(value, IDENTITY)))
        self.assertEqual([], value["open_pull_requests"])

    def test_a_peer_pull_request_is_reported_only_while_it_delivers_a_local_request(self) -> None:
        value = snapshot(
            [issue_item(7)],
            [
                pull_request(11, repository=PEER, closes=[(LOCAL, 7)]),
                pull_request(12, repository=PEER, body="Unrelated peer work"),
            ],
        )

        self.assertEqual(
            [f"github:{PEER}#11"],
            [value["provider_ref"] for value in value["open_pull_requests"]],
        )


class AmbiguousLinkTests(unittest.TestCase):
    def test_conflicting_kanbanlan_ids_fail_visibly_and_link_nothing(self) -> None:
        value = snapshot(
            [issue_item(7)],
            [
                pull_request(
                    11,
                    repository=PEER,
                    body=f"Delivers {IDENTITY} and {OTHER_IDENTITY}",
                    closes=[(LOCAL, 7)],
                )
            ],
        )
        item = resolve_request_item(value, IDENTITY)

        self.assertEqual(["closing_reference"], item["linked_open_pull_requests"][0]["linked_by"])
        problem = value["linkage_problems"][0]
        self.assertEqual("conflicting_kanbanlan_ids", problem["kind"])
        self.assertEqual([IDENTITY, OTHER_IDENTITY], sorted(problem["kanbanlan_ids"]))

    def test_a_declared_identity_survives_a_passing_mention_of_another(self) -> None:
        body = f"Kanbanlan: `{IDENTITY}`\n\nFollow-up work is tracked as {OTHER_IDENTITY}."
        value = snapshot([issue_item(7)], [pull_request(11, repository=PEER, body=body)])

        self.assertEqual([f"github:{PEER}#11"], linked_refs(resolve_request_item(value, IDENTITY)))
        self.assertEqual([], value["linkage_problems"])

    def test_a_duplicated_identity_across_repositories_links_nothing(self) -> None:
        value = snapshot(
            [issue_item(7), issue_item(9, repository=PEER)],
            [pull_request(11, repository=PEER, body=f"Kanbanlan: `{IDENTITY}`")],
            scope=SCOPE_PROJECT,
        )

        for item in value["items"]:
            with self.subTest(repository=item["repository"]):
                self.assertEqual([], linked_refs(item))
        problem = value["linkage_problems"][0]
        self.assertEqual("duplicate_kanbanlan_id", problem["kind"])
        self.assertIn(f"github:{LOCAL}#7", problem["detail"])
        self.assertIn(f"github:{PEER}#9", problem["detail"])

    def test_an_unknown_identity_is_simply_not_a_link(self) -> None:
        value = snapshot(
            [issue_item(7)],
            [pull_request(11, repository=PEER, body=f"Kanbanlan: `{OTHER_IDENTITY}`")],
        )

        self.assertEqual([], linked_refs(resolve_request_item(value, IDENTITY)))
        self.assertEqual([], value["linkage_problems"])


class CrossRepositoryReconciliationTests(unittest.TestCase):
    def test_an_open_cross_repository_pull_request_moves_the_card_to_review(self) -> None:
        value = snapshot([issue_item(7)], [pull_request(11, repository=PEER, closes=[(LOCAL, 7)])])
        label, projection, reason = expected_state(resolve_request_item(value, IDENTITY))

        self.assertEqual(("status:review", "In review"), (label, projection))
        self.assertIn("pull request", reason)

    def test_a_draft_cross_repository_pull_request_still_counts_as_open(self) -> None:
        value = snapshot(
            [issue_item(7)],
            [pull_request(11, repository=PEER, closes=[(LOCAL, 7)], is_draft=True)],
        )
        item = resolve_request_item(value, IDENTITY)

        self.assertTrue(item["linked_open_pull_requests"][0]["is_draft"])
        self.assertEqual("In review", expected_state(item)[1])

    def test_a_merged_cross_repository_pull_request_leaves_the_request_closed(self) -> None:
        """A merged pull request is no longer open, so it is not fetched.

        GitHub closes the request through the qualified closing reference, and
        a closed request is Done.
        """

        value = snapshot([issue_item(7, state="CLOSED", labels=["priority:p1"])], [])
        label, projection, _ = expected_state(resolve_request_item(value, IDENTITY))

        self.assertEqual((None, "Done"), (label, projection))

    def test_a_cross_repository_pull_request_closed_without_merge_releases_review(self) -> None:
        claim = {
            "body": "CLAIM: 2026-08-19T00:00:00Z\nSession: claude-1\nTouchpoints: src",
            "createdAt": "2026-08-19T00:00:01Z",
            "author": {"login": "agent"},
        }
        value = snapshot(
            [issue_item(7, status="In review", labels=["status:review"], comments=[claim])],
            [],
        )
        item = resolve_request_item(value, IDENTITY)
        label, projection, reason = expected_state(item)

        self.assertEqual(("status:in-progress", "In progress"), (label, projection))
        self.assertIn("CLAIM", reason)

    def test_reconciliation_does_not_move_unrelated_cards(self) -> None:
        value = snapshot(
            [
                issue_item(7, status="In progress", labels=["status:in-progress"]),
                issue_item(
                    9,
                    repository=PEER,
                    kanbanlan_id=OTHER_IDENTITY,
                    status="Ready",
                    labels=["status:ready"],
                ),
            ],
            [pull_request(11, repository=PEER, closes=[(LOCAL, 7)])],
            scope=SCOPE_PROJECT,
        )

        drift = plan_reconciliation(value, [])

        moved = {value.issue_number for value in drift if value.kind.startswith("set_")}
        self.assertEqual({7}, moved)


class ReviewCommandTests(unittest.TestCase):
    def run_review(self, value: dict[str, Any]) -> tuple[int, str, mock.Mock]:
        provider = mock.Mock()
        store = mock.Mock()
        store.refresh.return_value = value
        args = Namespace(
            command="review",
            issue=IDENTITY,
            json_output=False,
            repo_root=None,
            actor_session=None,
        )
        stream = StringIO()
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, store),
            ),
            mock.patch("kanbanlan.cli._actor_session", return_value=None),
            mock.patch("kanbanlan.cli._set_state"),
            mock.patch("kanbanlan.cli._record_session_activity") as activity,
            redirect_stdout(stream),
        ):
            code = _cmd_review(args)
        return code, stream.getvalue(), activity

    def test_review_accepts_a_qualifying_cross_repository_pull_request(self) -> None:
        claim = {
            "body": "CLAIM: 2026-08-19T00:00:00Z\nSession: claude-1\nTouchpoints: src",
            "createdAt": "2026-08-19T00:00:01Z",
            "author": {"login": "agent"},
        }
        value = snapshot(
            [issue_item(7, comments=[claim])],
            [pull_request(11, repository=PEER, closes=[(LOCAL, 7)])],
        )

        code, output, activity = self.run_review(value)

        self.assertEqual(0, code)
        self.assertIn(f"github:{PEER}#11", output)
        self.assertEqual("claude-1", activity.call_args.kwargs["owner_session"])

    def test_review_accepts_an_identity_only_cross_repository_pull_request(self) -> None:
        value = snapshot(
            [issue_item(7)],
            [pull_request(11, repository=PEER, body=f"Kanbanlan: `{IDENTITY}`")],
        )

        code, output, _ = self.run_review(value)

        self.assertEqual(0, code)
        self.assertIn("kanbanlan_id", output)

    def test_review_refuses_and_explains_an_ambiguous_reference(self) -> None:
        value = snapshot(
            [issue_item(7), issue_item(9, repository=PEER)],
            [pull_request(11, repository=PEER, body=f"Kanbanlan: `{IDENTITY}`")],
            scope=SCOPE_PROJECT,
        )
        provider = mock.Mock()
        store = mock.Mock()
        store.refresh.return_value = value
        args = Namespace(
            command="review",
            issue=f"{LOCAL}#7",
            json_output=False,
            repo_root=None,
            actor_session=None,
        )
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, store),
            ),
            mock.patch("kanbanlan.cli._actor_session", return_value=None),
            self.assertRaises(RuntimeError) as raised,
        ):
            _cmd_review(args)

        self.assertIn("duplicate_kanbanlan_id", str(raised.exception))

    def test_review_still_refuses_a_request_with_no_pull_request_at_all(self) -> None:
        value = snapshot([issue_item(7)], [])
        provider = mock.Mock()
        store = mock.Mock()
        store.refresh.return_value = value
        args = Namespace(
            command="review",
            issue=IDENTITY,
            json_output=False,
            repo_root=None,
            actor_session=None,
        )
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, store),
            ),
            mock.patch("kanbanlan.cli._actor_session", return_value=None),
            self.assertRaises(RuntimeError) as raised,
        ):
            _cmd_review(args)

        self.assertIn("no linked open pull request", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
