from __future__ import annotations

import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from kanbanlan import __version__
from kanbanlan.cli import (
    DEFAULT_TEMPLATE_NUMBER,
    DEFAULT_TEMPLATE_OWNER,
    ProjectChoice,
    _choose_project,
    _cmd_init,
    _cmd_upgrade,
    _materialize_project,
    _parse_template,
    _project_number,
    _project_reference,
    _prompt_bool,
    build_parser,
    main,
)
from kanbanlan.runner import CommandError, CommandResult


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

    def test_upgrade_uses_uv_tool_upgrade(self) -> None:
        runner = mock.Mock()
        args = build_parser().parse_args(["upgrade"])

        with mock.patch("kanbanlan.cli.Runner", return_value=runner):
            self.assertEqual(0, _cmd_upgrade(args))

        runner.run.assert_called_once_with(
            ["uv", "tool", "upgrade", "kanbanlan"],
            capture=False,
        )

    def test_upgrade_explains_when_uv_is_unavailable(self) -> None:
        runner = mock.Mock()
        runner.run.side_effect = FileNotFoundError
        args = build_parser().parse_args(["upgrade"])

        with (
            mock.patch("kanbanlan.cli.Runner", return_value=runner),
            self.assertRaisesRegex(RuntimeError, "uv is required"),
        ):
            _cmd_upgrade(args)

    def test_upgrade_explains_before_running_when_uv_is_not_installed(self) -> None:
        args = build_parser().parse_args(["upgrade"])

        with (
            mock.patch("kanbanlan.cli.shutil.which", return_value=None),
            self.assertRaisesRegex(RuntimeError, "uv is required"),
        ):
            _cmd_upgrade(args)

    def test_normal_commands_check_for_a_new_release(self) -> None:
        with (
            mock.patch("kanbanlan.cli.notify_if_update_available") as notify,
            mock.patch("kanbanlan.cli._cmd_status", return_value=0),
        ):
            self.assertEqual(0, main(["status"]))

        notify.assert_called_once_with(__version__)

    def test_upgrade_does_not_check_before_upgrading(self) -> None:
        with (
            mock.patch("kanbanlan.cli.notify_if_update_available") as notify,
            mock.patch("kanbanlan.cli._cmd_upgrade", return_value=0),
        ):
            self.assertEqual(0, main(["upgrade"]))

        notify.assert_not_called()

    def test_json_mode_does_not_check_for_updates(self) -> None:
        with (
            mock.patch("kanbanlan.cli.notify_if_update_available") as notify,
            mock.patch("kanbanlan.cli._cmd_status", return_value=0),
        ):
            self.assertEqual(0, main(["--json", "status"]))

        notify.assert_not_called()

    def test_created_project_number_falls_back_to_url(self) -> None:
        self.assertEqual(
            17,
            _project_number({"url": "https://github.com/orgs/acme/projects/17"}),
        )

    def test_project_sources_are_mutually_exclusive(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["init", "--project-number", "2", "--create-project"])

    def test_interactive_project_defaults_to_fresh_template(self) -> None:
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

        self.assertEqual("copy", choice.mode)
        self.assertEqual(DEFAULT_TEMPLATE_OWNER, choice.template_owner)
        self.assertEqual(DEFAULT_TEMPLATE_NUMBER, choice.template_number)
        self.assertEqual("Delivery Board", choice.title)
        github.copy_project.assert_not_called()

        github.copy_project.return_value = {"number": 8}
        self.assertEqual(8, _materialize_project(github, choice, "acme"))
        github.copy_project.assert_called_once_with(
            DEFAULT_TEMPLATE_OWNER,
            DEFAULT_TEMPLATE_NUMBER,
            "acme",
            "Delivery Board",
        )

    def test_interactive_project_list_still_defaults_to_fresh_template(self) -> None:
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

        with mock.patch("builtins.input", side_effect=["", ""]):
            choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual("copy", choice.mode)
        self.assertEqual(DEFAULT_TEMPLATE_OWNER, choice.template_owner)
        self.assertEqual(DEFAULT_TEMPLATE_NUMBER, choice.template_number)
        self.assertEqual("widget Delivery", choice.title)
        github.create_project.assert_not_called()

    def test_non_interactive_project_defaults_to_fresh_template(self) -> None:
        github = mock.Mock()
        args = Namespace(
            project_title=None,
            template_project=None,
            create_project=False,
            non_interactive=True,
        )

        choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual("copy", choice.mode)
        self.assertEqual(DEFAULT_TEMPLATE_OWNER, choice.template_owner)
        self.assertEqual(DEFAULT_TEMPLATE_NUMBER, choice.template_number)
        self.assertEqual("widget Delivery", choice.title)
        github.list_projects.assert_not_called()

    def test_explicit_empty_project_overrides_default_template(self) -> None:
        github = mock.Mock()
        args = Namespace(
            project_title="Custom Delivery",
            template_project=None,
            create_project=True,
            non_interactive=True,
        )

        choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual(ProjectChoice(mode="create", title="Custom Delivery"), choice)

    def test_explicit_existing_project_overrides_default_template(self) -> None:
        github = mock.Mock()
        args = Namespace(
            project_title=None,
            template_project=None,
            create_project=False,
            non_interactive=True,
        )

        choice = _choose_project(args, github, "acme", "widget", 9)

        self.assertEqual(ProjectChoice(mode="existing", number=9), choice)

    def test_explicit_template_overrides_default_template(self) -> None:
        github = mock.Mock()
        args = Namespace(
            project_title="Custom Delivery",
            template_project="acme/12",
            create_project=False,
            non_interactive=True,
        )

        choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual("copy", choice.mode)
        self.assertEqual("acme", choice.template_owner)
        self.assertEqual(12, choice.template_number)
        self.assertEqual("Custom Delivery", choice.title)

    def test_interactive_project_choice_retries_invalid_input(self) -> None:
        github = mock.Mock()
        github.list_projects.return_value = [{"number": 4, "title": "Delivery"}]
        args = Namespace(
            project_title=None,
            template_project=None,
            create_project=False,
            non_interactive=False,
        )

        with mock.patch("builtins.input", side_effect=["unknown", "0", "2"]):
            choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual(4, choice.number)

    def test_interactive_project_choice_uses_displayed_selection_number(self) -> None:
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

        with mock.patch("builtins.input", return_value="3"):
            choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual(9, choice.number)

    def test_command_failure_includes_actionable_hint(self) -> None:
        stderr = StringIO()
        failure = RuntimeError("network is unavailable")
        with (
            mock.patch("kanbanlan.cli.notify_if_update_available"),
            mock.patch("kanbanlan.cli._cmd_status", side_effect=failure),
            redirect_stderr(stderr),
        ):
            self.assertEqual(1, main(["status"]))

        self.assertIn("Error: network is unavailable", stderr.getvalue())

    def test_github_auth_failure_suggests_auth_helper(self) -> None:
        stderr = StringIO()
        failure = CommandError(
            CommandResult(("gh", "api", "graphql"), 1, "", "authentication token expired")
        )
        with (
            mock.patch("kanbanlan.cli.notify_if_update_available"),
            mock.patch("kanbanlan.cli._cmd_status", side_effect=failure),
            redirect_stderr(stderr),
        ):
            self.assertEqual(1, main(["status"]))

        self.assertIn("Hint: Run 'kanbanlan auth'", stderr.getvalue())

    def test_mistyped_command_suggests_the_closest_command(self) -> None:
        stderr = StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["stats"])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("Did you mean 'status'?", stderr.getvalue())

    def test_keyboard_interrupt_has_shell_standard_exit_code(self) -> None:
        stderr = StringIO()
        with (
            mock.patch("kanbanlan.cli.notify_if_update_available"),
            mock.patch("kanbanlan.cli._cmd_status", side_effect=KeyboardInterrupt),
            redirect_stderr(stderr),
        ):
            self.assertEqual(130, main(["status"]))

        self.assertIn("Error: cancelled", stderr.getvalue())

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
            "",  # Create a preconfigured Project
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
        github.copy_project.assert_not_called()
        github.link_project.assert_not_called()

    def test_non_interactive_init_copies_default_template_with_repo_title(self) -> None:
        root = Path("/tmp/kanbanlan-non-interactive-init-test")
        github = mock.Mock()
        github.repository_info.return_value = {
            "owner": {"login": "acme"},
            "defaultBranchRef": {"name": "main"},
        }
        github.copy_project.return_value = {"number": 8}
        github.ensure_status_options.return_value = False
        store = mock.Mock()
        args = build_parser().parse_args(
            [
                "init",
                "--repository",
                "acme/widget",
                "--owner-type",
                "organization",
                "--non-interactive",
                "--skip-reconcile",
                "--no-open",
            ]
        )

        with (
            mock.patch("kanbanlan.cli._root", return_value=root),
            mock.patch("kanbanlan.cli.discover_default_branch", return_value="main"),
            mock.patch("kanbanlan.cli.GitHub", return_value=github),
            mock.patch("kanbanlan.cli.scaffold_repository", return_value=[]),
            mock.patch("kanbanlan.cli.cache_dir", return_value=root / ".cache"),
            mock.patch("kanbanlan.cli.CacheStore", return_value=store),
        ):
            self.assertEqual(0, _cmd_init(args))

        github.copy_project.assert_called_once_with(
            DEFAULT_TEMPLATE_OWNER,
            DEFAULT_TEMPLATE_NUMBER,
            "acme",
            "widget Delivery",
        )
        github.link_project.assert_called_once_with()
        github.create_project.assert_not_called()
