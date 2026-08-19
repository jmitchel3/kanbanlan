from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.config import (
    Config,
    discover_repository,
    primary_worktree,
    resolve_config_path,
)


class ConfigTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        config = Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=2,
            default_branch="main",
            stage_branch="main",
            production_branch="prod",
            session_tracking=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".kanbanlan.toml").write_text(config.to_toml(), encoding="utf-8")
            loaded = Config.load(root)
        self.assertEqual(config, loaded)

    def test_session_tracking_defaults_off_and_environment_can_override_it(self) -> None:
        config = Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=2,
        )

        self.assertFalse(config.session_tracking_enabled({}))
        self.assertTrue(config.session_tracking_enabled({"KANBANLAN_SESSION_TRACKING": "true"}))
        self.assertFalse(
            Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=2,
                session_tracking=True,
            ).session_tracking_enabled({"KANBANLAN_SESSION_TRACKING": "off"})
        )

    def test_invalid_session_tracking_environment_is_rejected(self) -> None:
        config = Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=2,
        )

        with self.assertRaisesRegex(ValueError, "KANBANLAN_SESSION_TRACKING"):
            config.session_tracking_enabled({"KANBANLAN_SESSION_TRACKING": "sometimes"})

    def test_invalid_repository_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Config(
                repository="missing-owner",
                project_owner="acme",
                project_owner_type="organization",
                project_number=1,
            )

    def test_repository_discovery_supports_ssh_and_https(self) -> None:
        class FakeResult:
            stdout = "git@github.com:acme/widget.git\n"

        class FakeRunner:
            def run(self, _args: list[str]) -> FakeResult:
                return FakeResult()

        with mock.patch("kanbanlan.config.Runner", return_value=FakeRunner()):
            self.assertEqual("acme/widget", discover_repository(Path("/tmp")))

    def test_primary_worktree_is_stable_from_a_linked_checkout(self) -> None:
        runner = mock.Mock()
        runner.run.return_value.stdout = (
            "worktree /tmp/primary\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /tmp/linked\nHEAD def\nbranch refs/heads/work\n"
        )

        with mock.patch("kanbanlan.config.Runner", return_value=runner):
            result = primary_worktree(Path("/tmp/linked"))

        self.assertEqual(Path("/tmp/primary").resolve(), result)
        runner.run.assert_called_once_with(["git", "worktree", "list", "--porcelain"])

    def test_malformed_config_has_repair_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".kanbanlan.toml").write_text("[project\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "could not read.*kanbanlan.toml"):
                Config.load(root)

    def test_incomplete_config_has_repair_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".kanbanlan.toml").write_text("schema_version = 1\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "init --reconfigure"):
                Config.load(root)


class ResolveConfigPathTests(unittest.TestCase):
    """Linked worktrees only materialize tracked files.

    A repository that has not committed its ``.kanbanlan.toml`` leaves every
    linked worktree without one, which breaks the very workflow this tool
    prescribes (a dedicated worktree per request).
    """

    def _config(self) -> Config:
        return Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=2,
        )

    def test_local_config_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".kanbanlan.toml").write_text("x", encoding="utf-8")
            with mock.patch("kanbanlan.config.primary_worktree") as primary:
                self.assertEqual(resolve_config_path(root), root / ".kanbanlan.toml")
            primary.assert_not_called()

    def test_falls_back_to_the_primary_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary_root = base / "primary"
            linked = base / "linked"
            primary_root.mkdir()
            linked.mkdir()
            (primary_root / ".kanbanlan.toml").write_text("x", encoding="utf-8")

            with mock.patch("kanbanlan.config.primary_worktree", return_value=primary_root):
                self.assertEqual(resolve_config_path(linked), primary_root / ".kanbanlan.toml")

    def test_load_succeeds_from_a_linked_worktree(self) -> None:
        """End to end: the failure this fixes was Config.load, not path lookup."""
        config = self._config()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary_root = base / "primary"
            linked = base / "linked"
            primary_root.mkdir()
            linked.mkdir()
            (primary_root / ".kanbanlan.toml").write_text(config.to_toml(), encoding="utf-8")

            with mock.patch("kanbanlan.config.primary_worktree", return_value=primary_root):
                self.assertEqual(Config.load(linked), config)

    def test_primary_equal_to_root_reports_the_local_path(self) -> None:
        """Error messages must name the path the caller actually asked for."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("kanbanlan.config.primary_worktree", return_value=root):
                self.assertEqual(resolve_config_path(root), root / ".kanbanlan.toml")

    def test_missing_in_both_places_reports_the_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary_root = base / "primary"
            linked = base / "linked"
            primary_root.mkdir()
            linked.mkdir()

            with mock.patch("kanbanlan.config.primary_worktree", return_value=primary_root):
                self.assertEqual(resolve_config_path(linked), linked / ".kanbanlan.toml")

    def test_git_failure_does_not_mask_the_original_problem(self) -> None:
        """Outside git, or with git unavailable, report the caller's path."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "kanbanlan.config.primary_worktree",
                side_effect=RuntimeError("not a worktree"),
            ):
                self.assertEqual(resolve_config_path(root), root / ".kanbanlan.toml")

            with mock.patch(
                "kanbanlan.config.primary_worktree",
                side_effect=RuntimeError("not a worktree"),
            ):
                with self.assertRaisesRegex(RuntimeError, "is missing"):
                    Config.load(root)
