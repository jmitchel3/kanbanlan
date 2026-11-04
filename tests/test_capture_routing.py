from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from kanbanlan.cli import _cmd_capture, _place_captured_request, build_parser
from kanbanlan.config import Config, normalize_repository_target
from kanbanlan.github import GitHub
from kanbanlan.identity import attach_kanbanlan_id
from kanbanlan.runner import CommandError, CommandResult
from kanbanlan.snapshot import SCOPE_PROJECT, build_snapshot

LOCAL = "acme/widget"
PEER = "acme/website"
GENERATED_AT = datetime(2026, 8, 19, tzinfo=UTC)


def config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "repository": LOCAL,
        "project_owner": "acme",
        "project_owner_type": "organization",
        "project_number": 2,
    }
    values.update(overrides)
    return Config(**values)


def issue_item(number: int, kanbanlan_id: str, *, repository: str) -> dict[str, Any]:
    return {
        "id": f"item-{repository}-{number}",
        "type": "ISSUE",
        "isArchived": False,
        "fieldValues": {"nodes": [{"name": "Inbox", "field": {"name": "Status"}}]},
        "content": {
            "id": f"issue-{repository}-{number}",
            "number": number,
            "title": "Captured request",
            "body": attach_kanbanlan_id("Request body", kanbanlan_id),
            "url": f"https://github.test/{repository}/issues/{number}",
            "state": "OPEN",
            "stateReason": None,
            "createdAt": "2026-08-19T00:00:00Z",
            "updatedAt": "2026-08-19T01:00:00Z",
            "closedAt": None,
            "repository": {"nameWithOwner": repository},
            "labels": {"nodes": [{"name": "status:intake", "color": "000000"}]},
            "assignees": {"nodes": []},
            "comments": {"nodes": []},
        },
    }


def project_snapshot(kanbanlan_id: str, *, repository: str, number: int = 4) -> dict[str, Any]:
    project = {
        "id": "project-1",
        "number": 2,
        "title": "Delivery",
        "url": "https://github.test/orgs/acme/projects/2",
        "updatedAt": "2026-08-19T01:00:00Z",
        "fields": {"nodes": []},
        "items": [issue_item(number, kanbanlan_id, repository=repository)],
    }
    return build_snapshot(config(), project, [], {}, GENERATED_AT, scope=SCOPE_PROJECT)


STATUS_FIELD = {
    "id": "field-status",
    "name": "Status",
    "dataType": "SINGLE_SELECT",
    "options": [{"id": "opt-inbox", "name": "Inbox", "color": "GRAY", "description": ""}],
}
IDENTITY = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER = "KBL-BBBBBBBBBBBBBBBBBBBBBBBBBB"
CACHED = {"project": {"id": "project-1", "fields": {"nodes": [STATUS_FIELD]}}, "items": []}
FRESH_PROJECT = {"id": "project-1", "fields": {"nodes": [{**STATUS_FIELD, "id": "field-new"}]}}


class RepositoryTargetTests(unittest.TestCase):
    def test_owner_and_name_is_accepted_verbatim(self) -> None:
        self.assertEqual(PEER, normalize_repository_target(PEER, hostname="github.com"))
        self.assertEqual(PEER, normalize_repository_target(f"  {PEER}  ", hostname="github.com"))

    def test_a_url_on_the_configured_host_is_accepted(self) -> None:
        for value in (
            f"https://github.com/{PEER}",
            f"https://github.com/{PEER}.git",
            f"https://github.com/{PEER}/",
            f"git@github.com:{PEER}.git",
            f"ssh://git@github.com/{PEER}",
        ):
            with self.subTest(value=value):
                self.assertEqual(PEER, normalize_repository_target(value, hostname="github.com"))

    def test_a_url_on_another_host_is_refused(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            normalize_repository_target(
                f"https://github.enterprise.test/{PEER}", hostname="github.com"
            )

        self.assertIn("github.enterprise.test", str(raised.exception))

    def test_a_malformed_target_is_refused(self) -> None:
        for value in ("", "   ", "widget", "acme/widget/extra", "acme /widget"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    normalize_repository_target(value, hostname="github.com")


class StubRunner:
    def __init__(self, *, unlinkable: bool = False, labels: list[str] | None = None):
        self.calls: list[list[str]] = []
        self.unlinkable = unlinkable
        self.labels = labels or []

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if self.unlinkable and args[:3] == ["gh", "project", "link"]:
            raise CommandError(CommandResult(tuple(args), 1, "", "permission denied"))
        return CommandResult(tuple(args), 0, "", "")

    def json(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["gh", "label", "list"]:
            return [{"name": name} for name in self.labels]
        return []


class PrepareCaptureTargetTests(unittest.TestCase):
    def github(self, *, linked: list[str], accessible: bool = True, **kwargs: Any) -> GitHub:
        runner = StubRunner(**kwargs)
        github = GitHub(Path("/tmp"), config(), runner=runner)

        def graphql(query, variables, *, retry=False):
            if "repositoryOwner" in query or "defaultBranchRef" in query:
                if not accessible:
                    return {"repository": None}
                return {
                    "repository": {
                        "id": "repo-1",
                        "nameWithOwner": f"{variables['owner']}/{variables['repo']}",
                        "owner": {"__typename": "Organization", "login": variables["owner"]},
                        "defaultBranchRef": {"name": "main"},
                    }
                }
            return {
                "organization": {
                    "projectV2": {
                        "id": "project-1",
                        "title": "Delivery",
                        "url": "https://github.test/orgs/acme/projects/2",
                        "repositories": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"nameWithOwner": value} for value in linked],
                        },
                    }
                }
            }

        github.graphql = graphql  # type: ignore[method-assign]
        github.runner = runner
        return github

    def test_an_already_linked_target_is_not_relinked_but_labels_are_provisioned(self) -> None:
        github = self.github(linked=[LOCAL, PEER])

        result = github.prepare_capture_target(PEER)

        self.assertEqual(PEER, result["repository"])
        self.assertTrue(result["already_linked"])
        commands = github.runner.calls
        self.assertNotIn("link", [value[2] for value in commands if value[:2] == ["gh", "project"]])
        labels = [value for value in commands if value[:3] == ["gh", "label", "create"]]
        self.assertTrue(labels)
        self.assertTrue(all(PEER in value for value in labels))

    def test_an_unlinked_target_is_linked_to_the_configured_project(self) -> None:
        github = self.github(linked=[LOCAL])

        result = github.prepare_capture_target(PEER)

        self.assertFalse(result["already_linked"])
        link = next(
            value for value in github.runner.calls if value[:3] == ["gh", "project", "link"]
        )
        self.assertIn(PEER, link)

    def test_an_unlinkable_target_fails_before_anything_is_created(self) -> None:
        github = self.github(linked=[LOCAL], unlinkable=True)

        with self.assertRaises(RuntimeError) as raised:
            github.prepare_capture_target(PEER)

        self.assertIn("could not be linked", str(raised.exception))
        self.assertEqual([], [value for value in github.runner.calls if "issue" in value])

    def test_an_inaccessible_target_fails_before_anything_is_created(self) -> None:
        github = self.github(linked=[LOCAL], accessible=False)

        with self.assertRaises(RuntimeError) as raised:
            github.prepare_capture_target(PEER)

        self.assertIn("was not found", str(raised.exception))
        self.assertEqual([], github.runner.calls)


class FindRequestTests(unittest.TestCase):
    def find(self, recent: list[dict[str, Any]], searched: list[dict[str, Any]]):
        runner = StubRunner()

        def json_(args, **kwargs):
            runner.calls.append(list(args))
            return searched if "--search" in args else recent

        runner.json = json_  # type: ignore[method-assign]
        github = GitHub(Path("/tmp"), config(), runner=runner)
        github.runner = runner
        return github.find_request(IDENTITY, repository=PEER), runner.calls

    def issue(self, number: int, identity: str) -> dict[str, Any]:
        body = attach_kanbanlan_id("body", identity)
        return {
            "number": number,
            "url": f"https://x/{number}",
            "title": "T",
            "state": "OPEN",
            "body": body,
        }

    def test_a_just_created_issue_is_found_without_waiting_for_search(self) -> None:
        found, calls = self.find([self.issue(9, OTHER), self.issue(8, IDENTITY)], [])

        self.assertEqual((8, f"github:{PEER}#8"), (found["number"], found["provider_ref"]))
        self.assertEqual(1, len(calls))
        self.assertNotIn("--search", calls[0])

    def test_an_older_issue_is_found_by_search(self) -> None:
        found, calls = self.find([self.issue(9, OTHER)], [self.issue(2, IDENTITY)])

        self.assertEqual(2, found["number"])
        self.assertIn(f'"{IDENTITY}" in:body', calls[1])

    def test_a_body_that_only_mentions_the_id_is_not_a_match(self) -> None:
        mention = {**self.issue(5, OTHER), "body": f"see {IDENTITY}"}
        found, _ = self.find([mention], [mention])

        self.assertIsNone(found)


class CaptureRoutingTests(unittest.TestCase):
    def capture(
        self,
        argv: list[str],
        *,
        target: str,
        preparation: dict[str, Any] | None = None,
        projection_error: Exception | None = None,
        cached: dict[str, Any] | None = CACHED,
        json_output: bool = False,
    ) -> tuple[int, str, mock.Mock, mock.Mock]:
        provider = mock.Mock()
        provider.provider_name = "github"
        provider.capabilities.repository_routing = True
        provider.create_request.return_value = f"https://github.test/{target}/issues/4"
        provider.prepare_capture_target.return_value = preparation or {
            "repository": target,
            "already_linked": True,
            "project_url": "https://github.test/orgs/acme/projects/2",
        }
        provider.add_to_projection.return_value = {"id": "PVTI_4"}
        provider.projection_metadata.return_value = FRESH_PROJECT
        if projection_error is not None:
            provider.add_to_projection.side_effect = projection_error
        store = mock.Mock()
        store.snapshot.return_value = cached
        args = build_parser().parse_args((["--json"] if json_output else []) + argv)
        stream = StringIO()
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, store),
            ),
            mock.patch("kanbanlan.cli._actor_session", return_value=None),
            mock.patch(
                "kanbanlan.cli.new_kanbanlan_id",
                return_value="KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA",
            ),
            mock.patch("kanbanlan.cli._refresh_after_mutation") as refresh,
            redirect_stdout(stream),
        ):
            code = _cmd_capture(args)
        self.refresh_after_mutation = refresh
        return code, stream.getvalue(), provider, store

    def assert_board_never_read(self, provider: mock.Mock, store: mock.Mock) -> None:
        provider.snapshot.assert_not_called()
        provider.list_open_requests.assert_not_called()
        store.refresh.assert_not_called()

    def test_capture_defaults_to_this_repository_and_never_guesses(self) -> None:
        code, output, provider, store = self.capture(
            ["capture", "Add HSA/FSA page to the website"],
            target=LOCAL,
        )

        self.assertEqual(0, code)
        provider.prepare_capture_target.assert_not_called()
        self.assertEqual(LOCAL, provider.create_request.call_args.kwargs["repository"])
        self.assert_board_never_read(provider, store)
        self.refresh_after_mutation.assert_called_once()

    def test_an_explicit_target_is_prepared_before_the_request_is_created(self) -> None:
        code, output, provider, _ = self.capture(
            ["capture", "Add HSA/FSA page", "--repository", PEER],
            target=PEER,
        )

        self.assertEqual(0, code)
        provider.prepare_capture_target.assert_called_once_with(PEER)
        self.assertEqual(PEER, provider.create_request.call_args.kwargs["repository"])
        self.assertIn(PEER, output)

    def test_a_url_target_on_the_configured_host_is_accepted(self) -> None:
        code, _, provider, _ = self.capture(
            ["capture", "Add HSA/FSA page", "--repository", f"https://github.com/{PEER}"],
            target=PEER,
        )

        self.assertEqual(0, code)
        provider.prepare_capture_target.assert_called_once_with(PEER)

    def test_naming_this_repository_explicitly_uses_the_ordinary_path(self) -> None:
        code, _, provider, store = self.capture(
            ["capture", "Add an export audit log", "--repository", LOCAL],
            target=LOCAL,
        )

        self.assertEqual(0, code)
        provider.prepare_capture_target.assert_not_called()
        self.assert_board_never_read(provider, store)

    def test_a_routed_request_reaches_inbox_in_the_repository_that_owns_it(self) -> None:
        code, _, provider, store = self.capture(
            ["capture", "Add HSA/FSA page", "--repository", PEER],
            target=PEER,
        )

        self.assertEqual(0, code)
        self.assert_board_never_read(provider, store)
        provider.set_projection_status.assert_called_once_with("PVTI_4", CACHED["project"], "Inbox")
        self.refresh_after_mutation.assert_not_called()

    def test_capture_places_the_card_without_reading_the_board(self) -> None:
        code, _, provider, store = self.capture(["capture", "Add a page"], target=LOCAL)

        self.assertEqual(0, code)
        self.assert_board_never_read(provider, store)
        provider.add_to_projection.assert_called_once_with(f"https://github.test/{LOCAL}/issues/4")
        provider.set_projection_status.assert_called_once_with("PVTI_4", CACHED["project"], "Inbox")
        provider.projection_metadata.assert_not_called()
        provider.set_request_status.assert_not_called()

    def test_without_a_cached_board_only_the_project_fields_are_read(self) -> None:
        code, _, provider, store = self.capture(
            ["capture", "Add a page"], target=LOCAL, cached=None
        )

        self.assertEqual(0, code)
        self.assert_board_never_read(provider, store)
        provider.set_projection_status.assert_called_once_with("PVTI_4", FRESH_PROJECT, "Inbox")

    def test_stale_cached_field_ids_fall_back_to_fresh_project_fields(self) -> None:
        tried: list[dict[str, Any]] = []

        def set_status(item_id: str, project: dict[str, Any], status: str) -> None:
            tried.append(project)
            if project is CACHED["project"]:
                raise RuntimeError("Could not resolve to a node with the global id")

        provider = mock.Mock()
        provider.add_to_projection.return_value = {"id": "PVTI_9"}
        provider.projection_metadata.return_value = FRESH_PROJECT
        provider.set_projection_status.side_effect = set_status
        store = mock.Mock()
        store.snapshot.return_value = CACHED

        item = _place_captured_request(
            provider, store, f"https://github.test/{LOCAL}/issues/9", "KBL-X", LOCAL, "T"
        )

        self.assertEqual([CACHED["project"], FRESH_PROJECT], tried)
        self.assertEqual(
            (9, f"github:{LOCAL}#9", "PVTI_9", "Inbox"),
            (item["number"], item["provider_ref"], item["project_item_id"], item["status"]),
        )

    def test_a_missing_project_item_id_is_a_project_setup_failure(self) -> None:
        provider = mock.Mock()
        provider.add_to_projection.return_value = {}
        with self.assertRaises(RuntimeError) as raised:
            _place_captured_request(
                provider, mock.Mock(), f"https://github.test/{LOCAL}/issues/9", "K", LOCAL, "T"
            )

        self.assertIn("did not report the new Project item", str(raised.exception))

    def test_success_json_identifies_the_repository_and_canonical_request(self) -> None:
        code, output, _, _ = self.capture(
            ["capture", "Add HSA/FSA page", "--repository", PEER],
            target=PEER,
            json_output=True,
        )
        payload = json.loads(output)["result"]

        self.assertEqual(0, code)
        self.assertEqual(PEER, payload["repository"])
        self.assertEqual(f"github:{PEER}#4", payload["provider_ref"])
        self.assertEqual("KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA", payload["kanbanlan_id"])
        self.assertEqual(f"https://github.test/{PEER}/issues/4", payload["canonical_url"])
        self.assertTrue(payload["routed"])

    def test_a_failure_after_creation_names_the_request_and_a_safe_repair(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            self.capture(
                ["capture", "Add HSA/FSA page", "--repository", PEER],
                target=PEER,
                projection_error=RuntimeError("project item-add failed"),
            )

        message = str(raised.exception)
        self.assertIn(f"https://github.test/{PEER}/issues/4", message)
        self.assertIn("Do not run capture again", message)
        self.assertIn("kanbanlan reconcile --apply", message)
        self.assertIn(PEER, message)

    def test_routing_is_refused_when_the_canonical_home_cannot_support_it(self) -> None:
        provider = mock.Mock()
        provider.provider_name = "notion"
        provider.capabilities.repository_routing = False
        args = build_parser().parse_args(["capture", "Add a page", "--repository", PEER])
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, mock.Mock()),
            ),
            mock.patch("kanbanlan.cli._actor_session", return_value=None),
            self.assertRaises(RuntimeError) as raised,
        ):
            _cmd_capture(args)

        self.assertIn("repository routing", str(raised.exception))
        provider.create_request.assert_not_called()

    def test_a_target_on_another_host_is_refused_before_creation(self) -> None:
        provider = mock.Mock()
        provider.provider_name = "github"
        provider.capabilities.repository_routing = True
        args = build_parser().parse_args(
            ["capture", "Add a page", "--repository", f"https://github.enterprise.test/{PEER}"]
        )
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, mock.Mock()),
            ),
            mock.patch("kanbanlan.cli._actor_session", return_value=None),
            self.assertRaises(RuntimeError),
        ):
            _cmd_capture(args)

        provider.create_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
