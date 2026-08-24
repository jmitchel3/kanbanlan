from __future__ import annotations

import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from kanbanlan.cli import _cmd_close, build_parser
from kanbanlan.config import Config
from kanbanlan.domain import resolve_request_item
from kanbanlan.github import CLOSE_REASONS, GitHub
from kanbanlan.identity import attach_kanbanlan_id
from kanbanlan.snapshot import build_snapshot
from kanbanlan.workflow import expected_state

REPOSITORY = "acme/widget"
IDENTITY = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
GENERATED_AT = datetime(2026, 8, 19, tzinfo=UTC)
CLAIM = {
    "body": "CLAIM: 2026-08-19T00:00:00Z\nSession: claude-1\nTouchpoints: src",
    "createdAt": "2026-08-19T00:00:01Z",
    "author": {"login": "agent"},
}


def config() -> Config:
    return Config(
        repository=REPOSITORY,
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def issue_item(
    number: int,
    *,
    status: str = "In progress",
    state: str = "OPEN",
    labels: list[str] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"item-{number}",
        "type": "ISSUE",
        "isArchived": False,
        "fieldValues": {"nodes": [{"name": status, "field": {"name": "Status"}}]},
        "content": {
            "id": f"issue-{number}",
            "number": number,
            "title": f"Request {number}",
            "body": attach_kanbanlan_id("Request body", IDENTITY),
            "url": f"https://github.test/{REPOSITORY}/issues/{number}",
            "state": state,
            "stateReason": None,
            "createdAt": "2026-08-19T00:00:00Z",
            "updatedAt": "2026-08-19T01:00:00Z",
            "closedAt": None,
            "repository": {"nameWithOwner": REPOSITORY},
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


def pull_request(number: int, *, closes: list[int] | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Pull request {number}",
        "body": "",
        "url": f"https://github.test/{REPOSITORY}/pull/{number}",
        "repository": {"nameWithOwner": REPOSITORY},
        "headRefName": "work/example",
        "baseRefName": "main",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "createdAt": "2026-08-19T00:00:00Z",
        "updatedAt": "2026-08-19T01:00:00Z",
        "author": {"login": "agent"},
        "labels": {"nodes": []},
        "closingIssuesReferences": {
            "nodes": [
                {
                    "number": issue_number,
                    "url": f"https://github.test/{REPOSITORY}/issues/{issue_number}",
                    "repository": {"nameWithOwner": REPOSITORY},
                }
                for issue_number in (closes or [])
            ]
        },
    }


def snapshot(
    items: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return build_snapshot(config(), project(items), pull_requests or [], {}, GENERATED_AT)


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


class CloseCommandTests(unittest.TestCase):
    def run_close(
        self,
        value: dict[str, Any],
        *,
        not_planned: bool = False,
        force: bool = False,
    ) -> tuple[int, str, mock.Mock, mock.Mock, mock.Mock]:
        provider = mock.Mock()
        store = mock.Mock()
        store.refresh.return_value = value
        args = Namespace(
            command="close",
            issue=IDENTITY,
            reason="delivered without a pull request",
            not_planned=not_planned,
            force=force,
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
            mock.patch("kanbanlan.cli._set_state") as set_state,
            mock.patch("kanbanlan.cli._record_session_activity") as activity,
            redirect_stdout(stream),
        ):
            code = _cmd_close(args)
        return code, stream.getvalue(), provider, set_state, activity

    def test_close_ends_the_request_and_settles_the_projection(self) -> None:
        value = snapshot([issue_item(7)])

        code, output, provider, set_state, activity = self.run_close(value)

        self.assertEqual(0, code)
        self.assertIn("completed", output)
        self.assertEqual("completed", provider.close_request.call_args.kwargs["reason"])
        self.assertIn("CLOSED:", provider.close_request.call_args.kwargs["comment"])
        # A closed request carries no status label and rests in Done.
        self.assertEqual((None, "Done"), set_state.call_args.args[3:])
        self.assertEqual("close", activity.call_args.kwargs["action"])
        self.assertEqual("Done", activity.call_args.kwargs["to_status"])

    def test_close_records_a_dropped_request_as_not_planned(self) -> None:
        value = snapshot([issue_item(7)])

        code, output, provider, _, _ = self.run_close(value, not_planned=True)

        self.assertEqual(0, code)
        self.assertIn("not planned", output)
        self.assertEqual("not_planned", provider.close_request.call_args.kwargs["reason"])

    def test_close_releases_an_active_claim_before_closing(self) -> None:
        value = snapshot([issue_item(7, comments=[CLAIM])])

        code, output, provider, _, activity = self.run_close(value)

        self.assertEqual(0, code)
        body = provider.comment_request.call_args.args[1]
        self.assertTrue(body.startswith("RELEASED:"))
        self.assertIn("Session: claude-1", body)
        self.assertIn("claude-1", output)
        self.assertEqual("claude-1", activity.call_args.kwargs["owner_session"])

    def test_close_refuses_a_request_whose_pull_request_is_still_open(self) -> None:
        value = snapshot([issue_item(7)], [pull_request(11, closes=[7])])

        with self.assertRaises(RuntimeError) as raised:
            self.run_close(value)

        self.assertIn("#11", str(raised.exception))
        self.assertIn("--force", str(raised.exception))

    def test_close_forces_past_an_open_pull_request_when_asked(self) -> None:
        value = snapshot([issue_item(7)], [pull_request(11, closes=[7])])

        code, _, provider, _, _ = self.run_close(value, force=True)

        self.assertEqual(0, code)
        provider.close_request.assert_called_once()

    def test_close_refuses_a_request_that_is_already_closed(self) -> None:
        value = snapshot([issue_item(7, status="Done", state="CLOSED", labels=["priority:p1"])])

        with self.assertRaises(RuntimeError) as raised:
            self.run_close(value)

        self.assertIn("already closed", str(raised.exception))
        self.assertIn("reconcile", str(raised.exception))

    def test_close_refuses_a_canonical_home_that_cannot_close(self) -> None:
        value = snapshot([issue_item(7)])
        provider = mock.Mock()
        provider.provider_name = "example"
        provider.capabilities.request_closing = False
        store = mock.Mock()
        store.refresh.return_value = value
        args = Namespace(
            command="close",
            issue=IDENTITY,
            reason="obsolete",
            not_planned=False,
            force=False,
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
            _cmd_close(args)

        self.assertIn("does not support closing", str(raised.exception))

    def test_a_closed_request_reconciles_to_done_without_a_status_label(self) -> None:
        value = snapshot([issue_item(7, status="In progress", state="CLOSED")])

        label, projection, _ = expected_state(resolve_request_item(value, IDENTITY))

        self.assertEqual((None, "Done"), (label, projection))


class CloseParserTests(unittest.TestCase):
    def test_close_requires_a_reason(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["close", IDENTITY])

    def test_close_parses_its_flags(self) -> None:
        args = build_parser().parse_args(
            ["close", IDENTITY, "--reason", "duplicate", "--not-planned", "--force"]
        )

        self.assertEqual("close", args.command)
        self.assertTrue(args.not_planned)
        self.assertTrue(args.force)


class GitHubCloseTests(unittest.TestCase):
    def provider(self) -> tuple[GitHub, mock.Mock]:
        runner = mock.Mock()
        return GitHub(Path("/tmp"), config(), runner=runner), runner

    def test_close_request_passes_the_provider_reason_and_comment(self) -> None:
        provider, runner = self.provider()

        provider.close_request(7, reason="not_planned", comment="CLOSED: now — duplicate")

        args = runner.run.call_args.args[0]
        self.assertEqual(["gh", "issue", "close", "7"], args[:4])
        self.assertEqual("not planned", args[args.index("--reason") + 1])
        self.assertIn("--comment", args)

    def test_close_request_rejects_an_unsupported_reason(self) -> None:
        provider, runner = self.provider()

        with self.assertRaises(RuntimeError):
            provider.close_request(7, reason="abandoned")

        runner.run.assert_not_called()
        self.assertEqual({"completed", "not_planned"}, set(CLOSE_REASONS))


if __name__ == "__main__":
    unittest.main()
