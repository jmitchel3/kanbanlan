from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config, discover_repository, primary_worktree


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

    def test_rate_limit_floor_defaults_to_500_and_rejects_negatives(self) -> None:
        config = Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=2,
        )
        self.assertEqual(500, config.rate_limit_floor)
        with self.assertRaises(ValueError):
            Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=2,
                rate_limit_floor=-1,
            )

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
