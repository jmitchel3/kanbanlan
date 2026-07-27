from __future__ import annotations

import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from kanbanlan import __version__
from kanbanlan.cli import (
    _choose_project,
    _cmd_init,
    _materialize_project,
    _parse_template,
    _project_number,
    _project_reference,
    _prompt_bool,
    build_parser,
)


class CliTests(unittest.TestCase):
    def test_version_comes_from_package_metadata(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            with mock.patch("sys.stdout") as stdout:
                build_parser().parse_args(["--version"])

        self.assertEqual(0, raised.exception.code)
        stdout.write.assert_called_once_with(f"kanbanlan {__version__}\n")

    def test_project_url_is_parsed(self) -> None:
        args = Namespace(
            project_owner=None,
            project_number=None,
            project_url="https://github.com/orgs/paracord-clients/projects/2",
        )

        self.assertEqual(
            ("paracord-clients", 2),
            _project_reference(args, "repository-owner"),
        )

    def test_created_project_number_falls_back_to_url(self) -> None:
        self.assertEqual(
            17,
            _project_number({"url": "https://github.com/orgs/acme/projects/17"}),
        )

    def test_project_sources_are_mutually_exclusive(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["init", "--project-number", "2", "--create-project"])

    def test_interactive_project_creation_is_deferred_until_materialized(self) -> None:
        github = mock.Mock()
        github.list_projects.return_value = []
        args = Namespace(
            project_title=None,
            template_project=None,
            create_project=False,
            non_interactive=False,
        )

        with mock.patch("builtins.input", side_effect=["", "Delivery Board"]):
            choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual("create", choice.mode)
        self.assertEqual("Delivery Board", choice.title)
        github.create_project.assert_not_called()

        github.create_project.return_value = {"number": 8}
        self.assertEqual(8, _materialize_project(github, choice, "acme"))
        github.create_project.assert_called_once_with("acme", "Delivery Board")

    def test_interactive_project_list_defaults_to_first_project(self) -> None:
        github = mock.Mock()
        github.list_projects.return_value = [
            {"number": 4, "title": "Delivery"},
            {"number": 9, "title": "Roadmap"},
        ]
        args = Namespace(
            project_title=None,
            template_project=None,
            create_project=False,
            non_interactive=False,
        )

        with mock.patch("builtins.input", return_value=""):
            choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual(4, choice.number)
        github.create_project.assert_not_called()

    def test_interactive_project_choice_retries_invalid_input(self) -> None:
        github = mock.Mock()
        github.list_projects.return_value = [{"number": 4, "title": "Delivery"}]
        args = Namespace(
            project_title=None,
            template_project=None,
            create_project=False,
            non_interactive=False,
        )

        with mock.patch("builtins.input", side_effect=["unknown", "0", "9"]):
            choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual(9, choice.number)

    def test_template_reference_requires_a_positive_number(self) -> None:
        for value in ("acme", "acme/nope", "acme/0", "/2"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                _parse_template(value)

    def test_boolean_prompt_retries_invalid_answers(self) -> None:
        with mock.patch("builtins.input", side_effect=["perhaps", "yes"]):
            self.assertTrue(_prompt_bool("Continue?", default=False))

    def test_wizard_cancellation_does_not_create_or_configure_project(self) -> None:
        root = Path("/tmp/kanbanlan-wizard-test")
        github = mock.Mock()
        github.repository_info.return_value = {
            "owner": {"login": "acme"},
            "defaultBranchRef": {"name": "main"},
        }
        github.detect_owner_type.return_value = "organization"
        github.list_projects.return_value = []
        args = build_parser().parse_args(["init", "--repository", "acme/widget"])
        responses = [
            "",  # Project owner
            "",  # Create a new Project
            "",  # Default Project title
            "",  # Pull request target
            "",  # Staging branch
            "",  # No production branch
            "",  # Do not open the browser
            "no",  # Cancel at confirmation
        ]

        with (
            mock.patch("kanbanlan.cli._root", return_value=root),
            mock.patch("kanbanlan.cli.discover_default_branch", return_value="main"),
            mock.patch("kanbanlan.cli.GitHub", return_value=github),
            mock.patch("builtins.input", side_effect=responses),
        ):
            self.assertEqual(0, _cmd_init(args))

        github.create_project.assert_not_called()
        github.link_project.assert_not_called()
