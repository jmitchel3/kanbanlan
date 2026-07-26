from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config
from kanbanlan.github import GitHub


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
