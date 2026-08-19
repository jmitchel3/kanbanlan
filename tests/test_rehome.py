from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from kanbanlan.cli import COMMAND_NAMES, _cmd_rehome, build_parser
from kanbanlan.config import Config
from kanbanlan.domain import resolve_request_item
from kanbanlan.identity import attach_kanbanlan_id
from kanbanlan.rehome import plan_rehome
from kanbanlan.snapshot import SCOPE_PROJECT, build_snapshot

LOCAL = "acme/widget"
PEER = "acme/website"
IDENTITY = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
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
    repository: str,
    kanbanlan_id: str = IDENTITY,
    status: str = "Inbox",
    state: str = "OPEN",
    labels: list[str] | None = None,
    milestone: str | None = None,
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
            "title": "Add the HSA/FSA page",
            "body": attach_kanbanlan_id("Request body", kanbanlan_id),
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
                    for name in (labels or ["priority:p1", "status:intake"])
                ]
            },
            "milestone": {"title": milestone} if milestone else None,
            "assignees": {"nodes": []},
            "comments": {"nodes": comments or []},
        },
    }


def pull_request(number: int, *, repository: str, closes: list[tuple[str, int]]) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Implementation",
        "body": "",
        "url": f"https://github.test/{repository}/pull/{number}",
        "repository": {"nameWithOwner": repository},
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
                    "url": f"https://github.test/{issue_repository}/issues/{issue_number}",
                    "repository": {"nameWithOwner": issue_repository},
                }
                for issue_repository, issue_number in closes
            ]
        },
    }


def snapshot(items: list[dict[str, Any]], pull_requests: list[dict[str, Any]] | None = None):
    project = {
        "id": "project-1",
        "number": 2,
        "title": "Delivery",
        "url": "https://github.test/orgs/acme/projects/2",
        "updatedAt": "2026-08-19T01:00:00Z",
        "fields": {"nodes": []},
        "items": items,
    }
    return build_snapshot(
        config(), project, pull_requests or [], {}, GENERATED_AT, scope=SCOPE_PROJECT
    )


def inspection(*, linked: bool = True, missing: list[str] | None = None) -> dict[str, Any]:
    return {
        "repository": PEER,
        "already_linked": linked,
        "missing_labels": missing or [],
        "project_url": "https://github.test/orgs/acme/projects/2",
    }


class CommandRegistrationTests(unittest.TestCase):
    def test_every_command_is_listed_for_typo_suggestions(self) -> None:
        parser = build_parser()
        actions = [
            action
            for action in parser._subparsers._group_actions  # noqa: SLF001
            if action.choices
        ]
        registered = tuple(actions[0].choices)

        self.assertEqual(registered, COMMAND_NAMES)


class RehomePlanTests(unittest.TestCase):
    def plan(self, item_kwargs: dict[str, Any] | None = None, **kwargs: Any):
        items = [issue_item(7, repository=LOCAL, **(item_kwargs or {}))]
        value = snapshot(items, kwargs.pop("pull_requests", None))
        return plan_rehome(
            resolve_request_item(value, IDENTITY), PEER, kwargs.pop("inspection", inspection())
        )

    def test_a_clean_plan_preserves_identity_and_history(self) -> None:
        plan = self.plan()

        self.assertFalse(plan.blocked)
        self.assertEqual(IDENTITY, plan.kanbanlan_id)
        self.assertEqual(f"github:{LOCAL}#7", plan.source_provider_ref)
        self.assertEqual(PEER, plan.target_repository)
        self.assertIn("kanbanlan_id", plan.preserved)
        self.assertIn("comments", plan.preserved)
        self.assertIn("session_history", plan.preserved)

    def test_the_plan_reports_the_target_setup_it_would_perform(self) -> None:
        plan = self.plan(inspection=inspection(linked=False, missing=["status:ready"]))

        self.assertTrue(plan.target_link_required)
        self.assertEqual(("status:ready",), plan.labels_to_provision)

    def test_untransferable_fields_are_reported_as_dropped(self) -> None:
        plan = self.plan(
            {"milestone": "0.8.0", "labels": ["priority:p1", "status:intake", "area:site"]}
        )

        dropped = {(value["field"], value["value"]) for value in plan.dropped}
        self.assertIn(("milestone", "0.8.0"), dropped)
        self.assertIn(("label", "area:site"), dropped)
        self.assertNotIn(("label", "priority:p1"), dropped)
        self.assertNotIn(("label", "status:intake"), dropped)

    def test_an_active_claim_blocks_the_move_with_a_safe_sequence(self) -> None:
        claim = {
            "body": "CLAIM: 2026-08-19T00:00:00Z\nSession: claude-1\nTouchpoints: src",
            "createdAt": "2026-08-19T00:00:01Z",
            "author": {"login": "agent"},
        }
        plan = self.plan({"comments": [claim], "status": "In progress"})

        blocker = next(value for value in plan.blockers if value.kind == "active_claim")
        self.assertIn("claude-1", blocker.detail)
        self.assertIn("kanbanlan release", blocker.resolution)

    def test_a_linked_open_pull_request_blocks_the_move(self) -> None:
        plan = self.plan(pull_requests=[pull_request(11, repository=LOCAL, closes=[(LOCAL, 7)])])

        blocker = next(value for value in plan.blockers if value.kind == "linked_open_pull_request")
        self.assertIn(f"github:{LOCAL}#11", blocker.detail)

    def test_a_closed_request_blocks_the_move(self) -> None:
        plan = self.plan({"state": "CLOSED"})

        self.assertIn("request_not_open", [value.kind for value in plan.blockers])

    def test_moving_a_request_to_where_it_already_lives_blocks(self) -> None:
        value = snapshot([issue_item(7, repository=PEER)])
        plan = plan_rehome(resolve_request_item(value, IDENTITY), PEER, inspection())

        self.assertIn("same_repository", [value.kind for value in plan.blockers])

    def test_a_request_without_a_portable_identity_is_refused(self) -> None:
        item = issue_item(7, repository=LOCAL)
        item["content"]["body"] = "No identity here"
        value = snapshot([item])

        with self.assertRaises(RuntimeError) as raised:
            plan_rehome(value["items"][0], PEER, inspection())

        self.assertIn("portable identity", str(raised.exception))

    def test_the_plan_says_implementation_is_not_moved(self) -> None:
        plan = self.plan()

        joined = " ".join(plan.warnings)
        self.assertIn("branches", joined)
        self.assertIn("pull requests are not moved", joined)


class RehomeCommandTests(unittest.TestCase):
    def provider(
        self,
        *,
        before: list[dict[str, Any]],
        after: list[dict[str, Any]] | None = None,
        pull_requests: list[dict[str, Any]] | None = None,
        target_inspection: dict[str, Any] | None = None,
        reconcile_error: Exception | None = None,
    ) -> mock.Mock:
        provider = mock.Mock()
        provider.provider_name = "github"
        provider.capabilities.project_scope = True
        provider.capabilities.request_rehoming = True
        provider.inspect_repository_target.return_value = target_inspection or inspection()
        provider.transfer_request.return_value = f"https://github.test/{PEER}/issues/12"
        reads = [snapshot(before, pull_requests)]
        if after is not None:
            reads.append(snapshot(after, pull_requests))
        provider.snapshot.side_effect = lambda **_: reads[
            min(len(provider.snapshot.call_args_list) - 1, len(reads) - 1)
        ]
        if reconcile_error is not None:
            provider.set_projection_status.side_effect = reconcile_error
        return provider

    def run_rehome(self, provider: mock.Mock, argv: list[str]) -> tuple[int, str, mock.Mock]:
        store = mock.Mock()
        args = build_parser().parse_args(argv)
        stream = StringIO()
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, store),
            ),
            mock.patch("kanbanlan.cli._actor_session", return_value=None),
            redirect_stdout(stream),
        ):
            code = _cmd_rehome(args)
        return code, stream.getvalue(), store

    def test_a_plan_is_the_default_and_changes_nothing(self) -> None:
        provider = self.provider(before=[issue_item(7, repository=LOCAL)])

        code, output, store = self.run_rehome(provider, ["rehome", IDENTITY, "--repository", PEER])

        self.assertEqual(0, code)
        self.assertIn(f"github:{LOCAL}#7 -> {PEER}", output)
        self.assertIn("rerun with --apply", output)
        provider.transfer_request.assert_not_called()
        provider.prepare_repository_target.assert_not_called()
        store.refresh.assert_not_called()

    def test_a_blocked_plan_reports_two_without_mutating(self) -> None:
        provider = self.provider(
            before=[issue_item(7, repository=LOCAL)],
            pull_requests=[pull_request(11, repository=LOCAL, closes=[(LOCAL, 7)])],
        )

        code, output, _ = self.run_rehome(provider, ["rehome", IDENTITY, "--repository", PEER])

        self.assertEqual(2, code)
        self.assertIn("blocked (linked_open_pull_request)", output)
        provider.transfer_request.assert_not_called()

    def test_apply_transfers_the_request_and_restores_its_project_state(self) -> None:
        provider = self.provider(
            before=[issue_item(7, repository=LOCAL, status="Inbox")],
            after=[issue_item(12, repository=PEER, status="Inbox")],
        )

        code, output, store = self.run_rehome(
            provider, ["rehome", IDENTITY, "--repository", PEER, "--apply"]
        )

        self.assertEqual(0, code)
        provider.prepare_repository_target.assert_called_once_with(PEER)
        provider.transfer_request.assert_called_once_with(7, PEER, repository=LOCAL)
        provider.set_request_status.assert_called_once_with(12, "status:intake", repository=PEER)
        self.assertEqual("Inbox", provider.set_projection_status.call_args[0][2])
        self.assertIn(f"github:{LOCAL}#7 -> github:{PEER}#12", output)
        store.refresh.assert_called_once()

    def test_apply_json_names_both_provider_references_and_dropped_fields(self) -> None:
        provider = self.provider(
            before=[
                issue_item(
                    7,
                    repository=LOCAL,
                    milestone="0.8.0",
                    labels=["priority:p1", "status:intake", "area:site"],
                )
            ],
            after=[issue_item(12, repository=PEER)],
        )

        code, output, _ = self.run_rehome(
            provider, ["--json", "rehome", IDENTITY, "--repository", PEER, "--apply"]
        )
        payload = json.loads(output)["result"]

        self.assertEqual(0, code)
        self.assertEqual(f"github:{LOCAL}#7", payload["source"]["provider_ref"])
        self.assertEqual(f"github:{PEER}#12", payload["target"]["provider_ref"])
        self.assertEqual(12, payload["target"]["number"])
        self.assertEqual(f"https://github.test/{PEER}/issues/12", payload["target"]["url"])
        self.assertEqual(IDENTITY, payload["kanbanlan_id"])
        self.assertIn("comments", payload["preserved"])
        dropped = {(value["field"], value["value"]) for value in payload["dropped"]}
        self.assertIn(("milestone", "0.8.0"), dropped)
        self.assertIn(("label", "area:site"), dropped)

    def test_the_moved_request_is_still_found_by_its_original_identity(self) -> None:
        provider = self.provider(
            before=[issue_item(7, repository=LOCAL)],
            after=[issue_item(12, repository=PEER)],
        )
        self.run_rehome(provider, ["rehome", IDENTITY, "--repository", PEER, "--apply"])

        moved = resolve_request_item(snapshot([issue_item(12, repository=PEER)]), IDENTITY)

        self.assertEqual(PEER, moved["repository"])
        self.assertEqual(f"github:{PEER}#12", moved["provider_ref"])

    def test_an_identical_number_in_the_target_does_not_confuse_the_move(self) -> None:
        provider = self.provider(
            before=[
                issue_item(7, repository=LOCAL),
                issue_item(7, repository=PEER, kanbanlan_id="KBL-BBBBBBBBBBBBBBBBBBBBBBBBBB"),
            ],
            after=[
                issue_item(7, repository=PEER, kanbanlan_id="KBL-BBBBBBBBBBBBBBBBBBBBBBBBBB"),
                issue_item(12, repository=PEER),
            ],
        )

        code, output, _ = self.run_rehome(
            provider, ["rehome", IDENTITY, "--repository", PEER, "--apply"]
        )

        self.assertEqual(0, code)
        provider.transfer_request.assert_called_once_with(7, PEER, repository=LOCAL)
        provider.set_request_status.assert_called_once_with(12, "status:intake", repository=PEER)

    def test_a_failure_after_transfer_names_the_new_location_and_a_retryable_repair(self) -> None:
        provider = self.provider(
            before=[issue_item(7, repository=LOCAL)],
            after=[issue_item(12, repository=PEER)],
            reconcile_error=RuntimeError("Project item-edit failed"),
        )

        with self.assertRaises(RuntimeError) as raised:
            self.run_rehome(provider, ["rehome", IDENTITY, "--repository", PEER, "--apply"])

        message = str(raised.exception)
        self.assertIn(f"https://github.test/{PEER}/issues/12", message)
        self.assertIn("was not duplicated", message)
        self.assertIn(f"kanbanlan rehome {IDENTITY} --repository {PEER} --apply", message)
        provider.create_request.assert_not_called()

    def test_apply_refuses_a_blocked_move(self) -> None:
        provider = self.provider(
            before=[issue_item(7, repository=LOCAL)],
            pull_requests=[pull_request(11, repository=LOCAL, closes=[(LOCAL, 7)])],
        )

        with self.assertRaises(RuntimeError) as raised:
            self.run_rehome(provider, ["rehome", IDENTITY, "--repository", PEER, "--apply"])

        self.assertIn("linked_open_pull_request", str(raised.exception))
        provider.transfer_request.assert_not_called()

    def test_a_target_on_another_host_fails_before_any_read_or_mutation(self) -> None:
        provider = self.provider(before=[issue_item(7, repository=LOCAL)])

        with self.assertRaises(RuntimeError) as raised:
            self.run_rehome(
                provider,
                [
                    "rehome",
                    IDENTITY,
                    "--repository",
                    f"https://github.enterprise.test/{PEER}",
                    "--apply",
                ],
            )

        self.assertIn("github.enterprise.test", str(raised.exception))
        provider.snapshot.assert_not_called()
        provider.transfer_request.assert_not_called()

    def test_rehoming_is_refused_when_the_canonical_home_cannot_support_it(self) -> None:
        provider = self.provider(before=[issue_item(7, repository=LOCAL)])
        provider.provider_name = "notion"
        provider.capabilities.request_rehoming = False

        with self.assertRaises(RuntimeError) as raised:
            self.run_rehome(provider, ["rehome", IDENTITY, "--repository", PEER, "--apply"])

        self.assertIn("rehoming", str(raised.exception))
        provider.transfer_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
