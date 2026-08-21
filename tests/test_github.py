from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config
from kanbanlan.github import OWNER_QUERY, UPDATE_STATUS_FIELD, GitHub
from kanbanlan.runner import CommandError, CommandResult, RateLimitError


def config() -> Config:
    return Config(
        repository="acme/widget",
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


class StubGitHub(GitHub):
    def __init__(self, project):
        super().__init__(Path("/tmp"), config())
        self.project = project
        self.mutations = []

    def project_metadata(self):
        return self.project

    def graphql(self, query, variables, *, retry=False):
        self.mutations.append((query, variables))
        return {}


class GitHubTests(unittest.TestCase):
    def test_auth_does_not_reauthenticate_for_network_failure(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = CommandResult(
            ("gh", "auth", "status"),
            1,
            "",
            "error connecting to api.github.com",
        )
        github = GitHub(Path("/tmp"), runner=runner)

        with self.assertRaises(CommandError):
            github.ensure_auth()

        runner.run.assert_called_once()

    def test_auth_interactive_login_has_no_timeout(self) -> None:
        runner = mock.Mock()
        runner.run.side_effect = [
            CommandResult(("gh", "auth", "status"), 1, "", "not logged in"),
            CommandResult(("gh", "auth", "login"), 0, "", ""),
        ]
        github = GitHub(Path("/tmp"), runner=runner)

        github.ensure_auth()

        runner.run.assert_has_calls(
            [
                mock.call(
                    ["gh", "auth", "status", "--active", "--hostname", "github.com"],
                    check=False,
                ),
                mock.call(
                    [
                        "gh",
                        "auth",
                        "login",
                        "--hostname",
                        "github.com",
                        "--git-protocol",
                        "https",
                        "--web",
                        "--scopes",
                        "project",
                    ],
                    capture=False,
                    timeout=None,
                ),
            ]
        )

    def test_detect_owner_type_uses_repository_owner_without_partial_errors(self) -> None:
        github = GitHub(Path("/tmp"), runner=mock.Mock())
        github.graphql = mock.Mock(
            side_effect=[
                {"repositoryOwner": {"__typename": "User", "login": "monalisa"}},
                {"repositoryOwner": {"__typename": "Organization", "login": "github"}},
            ]
        )

        self.assertEqual("user", github.detect_owner_type("monalisa"))
        self.assertEqual("organization", github.detect_owner_type("github"))
        self.assertEqual(
            [
                mock.call(OWNER_QUERY, {"login": "monalisa"}, retry=True),
                mock.call(OWNER_QUERY, {"login": "github"}, retry=True),
            ],
            github.graphql.call_args_list,
        )

    def test_project_scope_probe_uses_at_me_for_user_owner(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = CommandResult(tuple(), 0, "{}", "")
        github = GitHub(Path("/tmp"), runner=runner)

        github.ensure_project_scope("monalisa", owner_type="user")

        runner.run.assert_called_once_with(
            [
                "gh",
                "project",
                "list",
                "--owner",
                "@me",
                "--limit",
                "1",
                "--format",
                "json",
            ],
            check=False,
            retry=True,
        )

    def test_project_scope_does_not_reauthenticate_for_unrelated_failure(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = CommandResult(
            ("gh", "project", "list"),
            1,
            "",
            "Could not resolve to an Organization",
        )
        github = GitHub(Path("/tmp"), runner=runner)

        with self.assertRaises(CommandError):
            github.ensure_project_scope("monalisa", owner_type="user")

        runner.run.assert_called_once()

    def test_create_project_uses_valid_gh_command(self) -> None:
        runner = mock.Mock()
        runner.json.return_value = {"number": 3}
        github = GitHub(Path("/tmp"), config(), runner=runner)

        self.assertEqual({"number": 3}, github.create_project("acme", "Delivery"))
        runner.json.assert_called_once_with(
            [
                "gh",
                "project",
                "create",
                "--owner",
                "acme",
                "--title",
                "Delivery",
                "--format",
                "json",
            ]
        )

    def test_graphql_retries_reads_but_never_mutations(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = CommandResult(
            ("gh", "api", "graphql"), 0, '{"data": {"ok": true}}', ""
        )
        github = GitHub(Path("/tmp"), config(), runner=runner)

        github.graphql(OWNER_QUERY, {"login": "acme"}, retry=True)
        self.assertTrue(runner.run.call_args.kwargs["retry"])

        # A mutation may have applied before the response was lost, so the
        # default must stay single-attempt.
        github.graphql(UPDATE_STATUS_FIELD, {"field": "abc", "options": []})
        self.assertFalse(runner.run.call_args.kwargs["retry"])

    def test_graphql_rate_limited_error_type_raises_rate_limit_error(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = CommandResult(
            ("gh", "api", "graphql"),
            0,
            '{"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}',
            "",
        )
        github = GitHub(Path("/tmp"), config(), runner=runner)

        with self.assertRaises(RateLimitError):
            github.graphql(OWNER_QUERY, {"login": "acme"})

    def test_graphql_rate_limited_command_failure_raises_rate_limit_error(self) -> None:
        runner = mock.Mock()
        runner.run.side_effect = CommandError(
            CommandResult(
                ("gh", "api", "graphql"),
                1,
                "",
                "gh: API rate limit exceeded for user ID 1 (HTTP 403)",
            )
        )
        github = GitHub(Path("/tmp"), config(), runner=runner)

        with self.assertRaises(RateLimitError):
            github.graphql(OWNER_QUERY, {"login": "acme"})

    def test_collect_tolerates_an_unreadable_peer_but_propagates_rate_limits(self) -> None:
        class PeerFetchGitHub(GitHub):
            def __init__(self, peer_error: Exception) -> None:
                super().__init__(Path("/tmp"), config())
                self.peer_error = peer_error

            def _fetch_project(self):
                project = {
                    "id": "p",
                    "number": 2,
                    "title": "Delivery",
                    "url": "url",
                    "repositories": {"nodes": [{"nameWithOwner": "acme/peer"}]},
                    "fields": {"nodes": []},
                    "items": [],
                }
                return project, {"remaining": 4000, "resetAt": "2026-01-01T00:00:00Z"}

            def _fetch_pull_requests(self, repository=None):
                if repository == "acme/peer":
                    raise self.peer_error
                return [], {"remaining": 4000, "resetAt": "2026-01-01T00:00:00Z"}

        tolerant = PeerFetchGitHub(RuntimeError("no access"))
        read = tolerant.collect()
        self.assertEqual(
            ["acme/peer"],
            [value["repository"] for value in read.unavailable_repositories],
        )

        # Out of quota fails the whole read; a snapshot silently missing a
        # peer repository would defeat cross-repository overlap checks.
        limited = PeerFetchGitHub(RateLimitError("API rate limit exceeded"))
        with self.assertRaises(RateLimitError):
            limited.collect()

    def test_graphql_other_errors_stay_plain_runtime_errors(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = CommandResult(
            ("gh", "api", "graphql"),
            0,
            '{"errors": [{"type": "NOT_FOUND", "message": "no such project"}]}',
            "",
        )
        github = GitHub(Path("/tmp"), config(), runner=runner)

        with self.assertRaises(RuntimeError) as caught:
            github.graphql(OWNER_QUERY, {"login": "acme"})
        self.assertNotIsInstance(caught.exception, RateLimitError)

    def test_project_creation_is_not_retried(self) -> None:
        runner = mock.Mock()
        runner.json.return_value = {"number": 3}
        github = GitHub(Path("/tmp"), config(), runner=runner)

        github.create_project("acme", "Delivery")

        self.assertNotIn("retry", runner.json.call_args.kwargs)

    def test_open_issue_listing_is_retried(self) -> None:
        runner = mock.Mock()
        runner.json.return_value = []
        github = GitHub(Path("/tmp"), config(), runner=runner)

        github.open_issues()

        self.assertTrue(runner.json.call_args.kwargs["retry"])

    def test_status_aliases_are_reused_without_clearing_ids(self) -> None:
        github = StubGitHub(
            {
                "fields": {
                    "nodes": [
                        {
                            "id": "status-field",
                            "name": "Status",
                            "options": [
                                {
                                    "id": "todo-id",
                                    "name": "Todo",
                                    "color": "GRAY",
                                    "description": "",
                                },
                                {
                                    "id": "doing-id",
                                    "name": "In Progress",
                                    "color": "YELLOW",
                                    "description": "",
                                },
                                {
                                    "id": "done-id",
                                    "name": "Done",
                                    "color": "PURPLE",
                                    "description": "",
                                },
                            ],
                        }
                    ]
                }
            }
        )
        self.assertTrue(github.ensure_status_options())
        options = github.mutations[0][1]["options"]
        by_name = {value["name"]: value for value in options}
        self.assertEqual("todo-id", by_name["Inbox"]["id"])
        self.assertEqual("doing-id", by_name["In progress"]["id"])
        self.assertEqual("done-id", by_name["Done"]["id"])

    def test_complete_status_field_is_unchanged(self) -> None:
        names = ["Inbox", "Ready", "In progress", "Blocked", "In review", "Done"]
        github = StubGitHub(
            {
                "fields": {
                    "nodes": [
                        {
                            "id": "status-field",
                            "name": "Status",
                            "options": [
                                {
                                    "id": name,
                                    "name": name,
                                    "color": "GRAY",
                                    "description": "",
                                }
                                for name in names
                            ],
                        }
                    ]
                }
            }
        )
        self.assertFalse(github.ensure_status_options())
        self.assertEqual([], github.mutations)
