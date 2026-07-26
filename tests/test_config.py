from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.config import Config, discover_repository


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
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".kanbanlan.toml").write_text(config.to_toml(), encoding="utf-8")
            loaded = Config.load(root)
        self.assertEqual(config, loaded)

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
