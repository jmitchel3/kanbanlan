from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config
from kanbanlan.github import OWNER_QUERY, GitHub
from kanbanlan.runner import CommandError, CommandResult


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

    def graphql(self, query, variables):
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
                mock.call(OWNER_QUERY, {"login": "monalisa"}),
                mock.call(OWNER_QUERY, {"login": "github"}),
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
