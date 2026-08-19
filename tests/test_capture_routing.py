from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from kanbanlan.cli import _cmd_capture, build_parser
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


class CaptureRoutingTests(unittest.TestCase):
    def capture(
        self,
        argv: list[str],
        *,
        target: str,
        preparation: dict[str, Any] | None = None,
        projection_error: Exception | None = None,
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
        if projection_error is not None:
            provider.add_to_projection.side_effect = projection_error
        store = mock.Mock()

        captured: dict[str, Any] = {}

        def snapshot(*, generated_at, scope):
            captured["scope"] = scope
            return project_snapshot(captured["kanbanlan_id"], repository=target)

        provider.snapshot.side_effect = snapshot

        def new_id() -> str:
            captured["kanbanlan_id"] = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
            return captured["kanbanlan_id"]

        argv = (["--json"] if json_output else []) + argv
        args = build_parser().parse_args(argv)
        stream = StringIO()
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/tmp"), config(), provider, store),
            ),
            mock.patch("kanbanlan.cli._actor_session", return_value=None),
            mock.patch("kanbanlan.cli.new_kanbanlan_id", side_effect=new_id),
            mock.patch(
                "kanbanlan.cli.apply_reconciliation",
                return_value=(
                    [],
                    project_snapshot("KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA", repository=LOCAL),
                ),
            ),
            redirect_stdout(stream),
        ):
            code = _cmd_capture(args)
        return code, stream.getvalue(), provider, store

    def test_capture_defaults_to_this_repository_and_never_guesses(self) -> None:
        code, output, provider, store = self.capture(
            ["capture", "Add HSA/FSA page to the website"],
            target=LOCAL,
        )

        self.assertEqual(0, code)
        provider.prepare_capture_target.assert_not_called()
        self.assertEqual(LOCAL, provider.create_request.call_args.kwargs["repository"])
        store.refresh.assert_called()

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
        store.refresh.assert_called()

    def test_a_routed_request_reaches_inbox_in_the_repository_that_owns_it(self) -> None:
        code, _, provider, store = self.capture(
            ["capture", "Add HSA/FSA page", "--repository", PEER],
            target=PEER,
        )

        self.assertEqual(0, code)
        provider.snapshot.assert_called_once()
        self.assertEqual(SCOPE_PROJECT, provider.snapshot.call_args.kwargs["scope"])
        provider.set_request_status.assert_called_once_with(4, "status:intake", repository=PEER)
        self.assertEqual("Inbox", provider.set_projection_status.call_args[0][2])
        store.refresh.assert_not_called()

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
