from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from kanbanlan import __version__
from kanbanlan.cli import (
    DEFAULT_TEMPLATE_NUMBER,
    DEFAULT_TEMPLATE_OWNER,
    ProjectChoice,
    _activate_worker,
    _choose_project,
    _cmd_init,
    _cmd_reconcile,
    _cmd_resume,
    _cmd_session_hook,
    _cmd_sessions,
    _cmd_upgrade,
    _cmd_worker,
    _materialize_project,
    _parse_template,
    _project_number,
    _project_reference,
    _prompt_bool,
    _record_session_activity,
    build_parser,
    main,
)
from kanbanlan.config import Config
from kanbanlan.registry import RegistryStore
from kanbanlan.runner import CommandError, CommandResult
from kanbanlan.sessions import AgentSession
from kanbanlan.workflow import Drift


class CliTests(unittest.TestCase):
    def test_session_tracking_cli_options_are_explicit(self) -> None:
        init = build_parser().parse_args(["init", "--session-tracking"])
        disabled = build_parser().parse_args(["init", "--no-session-tracking"])
        reconfigure = build_parser().parse_args(["init", "--reconfigure"])
        claim = build_parser().parse_args(
            [
                "claim",
                "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA",
                "--touchpoints",
                "src",
                "--actor-session",
                "codex:019f-test",
            ]
        )

        self.assertTrue(init.session_tracking)
        self.assertFalse(disabled.session_tracking)
        self.assertTrue(reconfigure.reconfigure)
        self.assertEqual("codex:019f-test", claim.actor_session)

    def test_sessions_displays_native_id_with_harness_and_resume_command(self) -> None:
        item = self._session_item()
        store = mock.Mock()
        store.ensure.return_value = {"items": [item]}
        args = build_parser().parse_args(["sessions", item["kanbanlan_id"]])
        output = StringIO()

        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/repo"), mock.Mock(), mock.Mock(), store),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(0, _cmd_sessions(args))

        self.assertIn("019f-test · codex", output.getvalue())
        self.assertIn("codex resume 019f-test", output.getvalue())

    def test_resume_selects_handoff_recipient_as_responsible_session(self) -> None:
        item = self._session_item()
        item["session_history"].append(
            {
                "action": "handoff",
                "at": "2026-07-29T12:01:00Z",
                "actor": {
                    "display": "019f-test · codex",
                    "resume_command": ["codex", "resume", "019f-test"],
                },
                "responsible": {
                    "display": "claude-test · claude",
                    "resume_command": ["claude", "--resume", "claude-test"],
                },
            }
        )
        store = mock.Mock()
        store.ensure.return_value = {"items": [item]}
        args = build_parser().parse_args(["resume", item["kanbanlan_id"], "--action", "handoff"])
        output = StringIO()

        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path("/repo"), mock.Mock(), mock.Mock(), store),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(0, _cmd_resume(args))

        self.assertEqual("claude --resume claude-test", output.getvalue().strip())

    def test_session_hook_registers_native_context_only_when_enabled(self) -> None:
        root = Path("/repo")
        config = Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=2,
            session_tracking=True,
        )
        context = mock.Mock()
        args = build_parser().parse_args(["session-hook", "--agent", "agy"])
        output = StringIO()

        with (
            mock.patch("kanbanlan.cli._root", return_value=root),
            mock.patch("kanbanlan.cli.Config.load", return_value=config),
            mock.patch("kanbanlan.cli.cache_dir", return_value=Path("/cache")),
            mock.patch("kanbanlan.cli.SessionContextStore", return_value=context),
            mock.patch("sys.stdin", StringIO('{"conversationId":"agy-test","cwd":"/repo"}')),
            redirect_stdout(output),
        ):
            self.assertEqual(0, _cmd_session_hook(args))

        registered = context.register.call_args.args[0]
        self.assertEqual("agy-test · agy", registered.display)
        self.assertEqual("{}", output.getvalue().strip())

    def test_activity_comment_is_opt_in(self) -> None:
        provider = mock.Mock()
        session = AgentSession("codex", "019f-test", "test")
        disabled = Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=2,
        )
        enabled = Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=2,
            session_tracking=True,
        )

        _record_session_activity(
            config=disabled,
            provider=provider,
            reference=1,
            action="triage",
            from_status="Inbox",
            to_status="Ready",
            actor=session,
        )
        provider.comment_request.assert_not_called()

        _record_session_activity(
            config=enabled,
            provider=provider,
            reference=1,
            action="triage",
            from_status="Inbox",
            to_status="Ready",
            actor=session,
        )
        self.assertIn("019f-test · codex", provider.comment_request.call_args.args[1])

    @staticmethod
    def _session_item() -> dict:
        return {
            "type": "ISSUE",
            "number": 1,
            "title": "Tracked request",
            "kanbanlan_id": "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA",
            "provider_ref": "github:acme/widget#1",
            "session_history": [
                {
                    "action": "triage",
                    "at": "2026-07-29T12:00:00Z",
                    "to_status": "Ready",
                    "actor": {
                        "display": "019f-test · codex",
                        "resume_command": ["codex", "resume", "019f-test"],
                    },
                }
            ],
            "session_history_truncated": False,
        }

    def test_worker_lifecycle_parser_accepts_scoped_account(self) -> None:
        args = build_parser().parse_args(
            ["worker", "enable", "--github-login", "alice", "--interval", "60"]
        )
        self.assertEqual("worker", args.command)
        self.assertEqual("enable", args.action)
        self.assertEqual("alice", args.github_login)
        self.assertEqual(60, args.interval)

    def test_activation_respects_explicit_disablement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            root = Path(directory) / "repo"
            common = Path(directory) / "common"
            store = RegistryStore(state)
            store.register(
                common_dir=common,
                root=root,
                repository="acme/repo",
                hostname="github.com",
                github_login="alice",
            )
            store.disable(common)
            config = mock.Mock(hostname="github.com", repository="acme/repo")
            with (
                mock.patch.dict("os.environ", {"KANBANLAN_STATE_DIR": str(state)}),
                mock.patch("kanbanlan.cli.common_dir", return_value=common),
                mock.patch("kanbanlan.cli.primary_worktree") as primary_worktree,
                mock.patch("kanbanlan.cli.start_worker") as start,
            ):
                _activate_worker(root, config)
            start.assert_not_called()
            primary_worktree.assert_not_called()

    def test_activation_registers_the_primary_worktree_and_starts_one_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            linked = Path(directory) / "linked"
            primary = Path(directory) / "primary"
            common = primary / ".git"
            primary.mkdir()
            config = mock.Mock(hostname="github.com", repository="acme/repo")
            with (
                mock.patch.dict("os.environ", {"KANBANLAN_STATE_DIR": str(state)}),
                mock.patch("kanbanlan.cli.common_dir", return_value=common),
                mock.patch("kanbanlan.cli.primary_worktree", return_value=primary),
                mock.patch("kanbanlan.cli._discover_github_login", return_value="alice"),
                mock.patch("kanbanlan.cli.start_worker") as start,
            ):
                _activate_worker(linked, config)

            registration = RegistryStore(state).registrations()[0]
            self.assertEqual(str(primary.resolve()), registration.root)
            self.assertEqual("alice", registration.github_login)
            start.assert_called_once()

    def test_worker_disable_creates_a_tombstone_before_first_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            root = Path(directory) / "repo"
            common = root / ".git"
            root.mkdir()
            config = mock.Mock(hostname="github.com", repository="acme/repo")
            args = build_parser().parse_args(["worker", "disable"])
            with (
                mock.patch.dict("os.environ", {"KANBANLAN_STATE_DIR": str(state)}),
                mock.patch(
                    "kanbanlan.cli._context", return_value=(root, config, mock.Mock(), mock.Mock())
                ),
                mock.patch("kanbanlan.cli.common_dir", return_value=common),
                mock.patch("kanbanlan.cli.primary_worktree", return_value=root),
                mock.patch("kanbanlan.cli.worker_status", return_value={"worker": {}}),
            ):
                self.assertEqual(0, _cmd_worker(args))

            registration = RegistryStore(state).registrations()[0]
            self.assertFalse(registration.enabled)
            self.assertTrue(registration.disabled)

    def test_successful_reconcile_activates_worker_but_preview_drift_does_not(self) -> None:
        root = Path("/tmp/kanbanlan-reconcile-activation")
        config = mock.Mock()
        provider = mock.Mock()
        provider.list_open_requests.return_value = []
        store = mock.Mock()
        store.refresh.return_value = {"items": []}
        args = build_parser().parse_args(["reconcile"])
        with (
            mock.patch("kanbanlan.cli._context", return_value=(root, config, provider, store)),
            mock.patch("kanbanlan.cli._root", return_value=root),
            mock.patch("kanbanlan.cli.Config.load", return_value=config),
            mock.patch("kanbanlan.cli.plan_reconciliation", return_value=[]),
            mock.patch("kanbanlan.cli._activate_worker") as activate,
        ):
            self.assertEqual(0, _cmd_reconcile(args))
        activate.assert_called_once_with(root, config)

        drift = Drift("set_request_status", 1, "status:intake", "status:ready", "test")
        with (
            mock.patch("kanbanlan.cli._context", return_value=(root, config, provider, store)),
            mock.patch("kanbanlan.cli.plan_reconciliation", return_value=[drift]),
            mock.patch("kanbanlan.cli._activate_worker") as activate,
        ):
            self.assertEqual(2, _cmd_reconcile(args))
        activate.assert_not_called()

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
            timeout=None,
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

    def test_interactive_default_template_preserves_explicit_project_title(self) -> None:
        github = mock.Mock()
        github.list_projects.return_value = []
        args = Namespace(
            project_title="Explicit Delivery",
            template_project=None,
            create_project=False,
            non_interactive=False,
        )

        with mock.patch("builtins.input", side_effect=[""]) as prompt:
            choice = _choose_project(args, github, "acme", "widget", None)

        self.assertEqual("copy", choice.mode)
        self.assertEqual(DEFAULT_TEMPLATE_OWNER, choice.template_owner)
        self.assertEqual(DEFAULT_TEMPLATE_NUMBER, choice.template_number)
        self.assertEqual("Explicit Delivery", choice.title)
        prompt.assert_called_once()

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

    def test_failed_command_is_reported_as_a_runnable_command(self) -> None:
        stderr = StringIO()
        failure = CommandError(
            CommandResult(
                (
                    "gh",
                    "project",
                    "copy",
                    "6",
                    "--title",
                    "prevenir-automations Delivery",
                ),
                1,
                "",
                "unknown owner type",
            )
        )
        with (
            mock.patch("kanbanlan.cli.notify_if_update_available"),
            mock.patch("kanbanlan.cli._cmd_status", side_effect=failure),
            redirect_stderr(stderr),
        ):
            self.assertEqual(1, main(["status"]))

        output = stderr.getvalue()
        self.assertIn("--title 'prevenir-automations Delivery'", output)
        self.assertNotIn("--title prevenir-automations Delivery", output)

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

    def test_existing_init_enables_session_tracking_without_project_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
                default_branch="trunk",
                stage_branch="staging",
                production_branch="production",
                stale_seconds=240,
            )
            (root / ".kanbanlan.toml").write_text(original.to_toml(), encoding="utf-8")
            args = build_parser().parse_args(["init", "--session-tracking", "--non-interactive"])

            with (
                mock.patch("kanbanlan.cli._root", return_value=root),
                mock.patch("kanbanlan.cli.discover_repository") as discover_repository,
                mock.patch("kanbanlan.cli.discover_default_branch") as discover_default_branch,
                mock.patch("kanbanlan.cli.GitHub") as github,
            ):
                self.assertEqual(0, _cmd_init(args))

            updated = Config.load(root)
            self.assertEqual(7, updated.project_number)
            self.assertEqual("trunk", updated.default_branch)
            self.assertEqual("staging", updated.stage_branch)
            self.assertEqual("production", updated.production_branch)
            self.assertEqual(240, updated.stale_seconds)
            self.assertTrue(updated.session_tracking)
            self.assertTrue((root / ".codex" / "hooks.json").exists())
            discover_repository.assert_not_called()
            discover_default_branch.assert_not_called()
            github.assert_not_called()

    def test_existing_init_can_disable_tracking_without_deleting_hook_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
                session_tracking=True,
            )
            (root / ".kanbanlan.toml").write_text(original.to_toml(), encoding="utf-8")
            custom_hook = root / ".codex" / "hooks.json"
            custom_hook.parent.mkdir(parents=True)
            custom_hook.write_text('{"custom": true}\n', encoding="utf-8")
            args = build_parser().parse_args(["init", "--no-session-tracking", "--non-interactive"])

            with mock.patch("kanbanlan.cli._root", return_value=root):
                self.assertEqual(0, _cmd_init(args))

            self.assertFalse(Config.load(root).session_tracking)
            self.assertEqual('{"custom": true}\n', custom_hook.read_text(encoding="utf-8"))

    def test_existing_init_applies_local_overrides_and_preserves_unspecified_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
                default_branch="main",
                stage_branch="staging",
                production_branch="production",
                stale_seconds=180,
            )
            (root / ".kanbanlan.toml").write_text(original.to_toml(), encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "init",
                    "--default-branch",
                    "trunk",
                    "--production-branch",
                    "",
                    "--hostname",
                    "github.example.test",
                    "--stale-seconds",
                    "60",
                    "--non-interactive",
                ]
            )

            with (
                mock.patch("kanbanlan.cli._root", return_value=root),
                mock.patch("kanbanlan.cli.GitHub") as github,
            ):
                self.assertEqual(0, _cmd_init(args))

            updated = Config.load(root)
            self.assertEqual("trunk", updated.default_branch)
            self.assertEqual("staging", updated.stage_branch)
            self.assertEqual("", updated.production_branch)
            self.assertEqual("github.example.test", updated.hostname)
            self.assertEqual(60, updated.stale_seconds)
            self.assertFalse(updated.session_tracking)
            self.assertEqual(7, updated.project_number)
            github.assert_not_called()

    def test_existing_init_without_overrides_refreshes_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
            )
            (root / ".kanbanlan.toml").write_text(original.to_toml(), encoding="utf-8")
            args = build_parser().parse_args(["init", "--non-interactive"])
            output = StringIO()

            with (
                mock.patch("kanbanlan.cli._root", return_value=root),
                mock.patch("kanbanlan.cli.GitHub") as github,
                redirect_stdout(output),
            ):
                self.assertEqual(0, _cmd_init(args))

            self.assertEqual(original, Config.load(root))
            self.assertIn("refreshed in place", output.getvalue())
            github.assert_not_called()

    def test_existing_init_explains_reuse_and_can_be_cancelled_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
            )
            config_path = root / ".kanbanlan.toml"
            config_path.write_text(original.to_toml(), encoding="utf-8")
            args = build_parser().parse_args(["init", "--session-tracking"])
            output = StringIO()

            with (
                mock.patch("kanbanlan.cli._root", return_value=root),
                mock.patch("kanbanlan.cli.scaffold_repository") as scaffold,
                mock.patch("builtins.input", return_value="no"),
                redirect_stdout(output),
            ):
                self.assertEqual(0, _cmd_init(args))

            self.assertIn("Existing configuration detected", output.getvalue())
            self.assertIn("Project", output.getvalue())
            self.assertEqual(original, Config.load(root))
            scaffold.assert_not_called()

    def test_existing_init_requires_reconfigure_for_project_binding_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
            )
            (root / ".kanbanlan.toml").write_text(original.to_toml(), encoding="utf-8")
            args = build_parser().parse_args(["init", "--project-number", "9", "--non-interactive"])

            with (
                mock.patch("kanbanlan.cli._root", return_value=root),
                self.assertRaisesRegex(RuntimeError, "--reconfigure"),
            ):
                _cmd_init(args)

            self.assertEqual(original, Config.load(root))

    def test_reconfigure_explicitly_runs_full_setup_for_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
            )
            (root / ".kanbanlan.toml").write_text(original.to_toml(), encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "init",
                    "--reconfigure",
                    "--repository",
                    "acme/widget",
                    "--project-number",
                    "9",
                    "--owner-type",
                    "organization",
                    "--local-only",
                    "--non-interactive",
                ]
            )

            with (
                mock.patch("kanbanlan.cli._root", return_value=root),
                mock.patch("kanbanlan.cli.discover_default_branch", return_value="main"),
            ):
                self.assertEqual(0, _cmd_init(args))

            self.assertEqual(9, Config.load(root).project_number)

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
            mock.patch("kanbanlan.cli._activate_worker") as activate,
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
        activate.assert_not_called()

    def test_successful_live_init_activates_worker(self) -> None:
        root = Path("/tmp/kanbanlan-live-init-test")
        github = mock.Mock()
        github.repository_info.return_value = {
            "owner": {"login": "acme"},
            "defaultBranchRef": {"name": "main"},
        }
        github.copy_project.return_value = {"number": 8}
        github.ensure_status_options.return_value = False
        store = mock.Mock()
        store.refresh.return_value = {"items": []}
        args = build_parser().parse_args(
            [
                "init",
                "--repository",
                "acme/widget",
                "--owner-type",
                "organization",
                "--non-interactive",
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
            mock.patch("kanbanlan.cli.plan_reconciliation", return_value=[]),
            mock.patch("kanbanlan.cli._activate_worker") as activate,
        ):
            self.assertEqual(0, _cmd_init(args))

        activate.assert_called_once()
        self.assertEqual(root, activate.call_args.args[0])
        self.assertEqual("acme/widget", activate.call_args.args[1].repository)
