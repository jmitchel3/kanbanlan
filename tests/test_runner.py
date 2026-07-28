from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from kanbanlan.runner import CommandError, CommandResult, Runner


class RunnerTests(unittest.TestCase):
    def test_runner_env_overrides_without_dropping_inherited_environment(self) -> None:
        runner = Runner(env={"KANBANLAN_TEST_ENV": "scoped"})
        with mock.patch("kanbanlan.runner.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            runner.run(["true"])

        environment = run.call_args.kwargs["env"]
        self.assertEqual("scoped", environment["KANBANLAN_TEST_ENV"])
        self.assertEqual(os.environ.get("PATH"), environment.get("PATH"))

    def test_runner_env_can_remove_ambient_credentials(self) -> None:
        runner = Runner(env={"GH_TOKEN": None})
        with (
            mock.patch.dict(os.environ, {"GH_TOKEN": "ambient"}, clear=False),
            mock.patch("kanbanlan.runner.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            runner.run(["true"])

        self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])

    def test_runner_uses_default_timeout(self) -> None:
        runner = Runner()
        with mock.patch("kanbanlan.runner.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            runner.run(["true"])

        self.assertEqual(60.0, run.call_args.kwargs["timeout"])

    def test_runner_allows_timeout_opt_out(self) -> None:
        runner = Runner()
        with mock.patch("kanbanlan.runner.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            runner.run(["interactive"], capture=False, timeout=None)

        self.assertIsNone(run.call_args.kwargs["timeout"])

    def test_runner_converts_timeout_to_command_error(self) -> None:
        runner = Runner()
        expired = subprocess.TimeoutExpired(
            cmd=["gh", "api"],
            timeout=2.5,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )
        with (
            mock.patch("kanbanlan.runner.subprocess.run", side_effect=expired),
            self.assertRaises(CommandError) as raised,
        ):
            runner.run(["gh", "api"], timeout=2.5)

        result = raised.exception.result
        self.assertEqual(("gh", "api"), result.args)
        self.assertEqual(124, result.returncode)
        self.assertEqual("partial stdout", result.stdout)
        self.assertEqual("partial stderr\ntimed out after 2.5 seconds", result.stderr)
        self.assertIn("gh api", str(raised.exception))

    def test_invalid_json_error_includes_bounded_command_output(self) -> None:
        runner = Runner()
        runner.run = mock.Mock(
            return_value=CommandResult(("gh", "api"), 0, "not json\nwith context", "")
        )

        with self.assertRaisesRegex(RuntimeError, "not json with context"):
            runner.json(["gh", "api"])

    def test_invalid_json_error_explains_empty_output(self) -> None:
        runner = Runner()
        runner.run = mock.Mock(return_value=CommandResult(("gh", "api"), 0, "", ""))

        with self.assertRaisesRegex(RuntimeError, "empty output"):
            runner.json(["gh", "api"])
