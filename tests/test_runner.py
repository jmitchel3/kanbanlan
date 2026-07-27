from __future__ import annotations

import os
import unittest
from unittest import mock

from kanbanlan.runner import CommandResult, Runner


class RunnerTests(unittest.TestCase):
    def test_runner_env_overrides_without_dropping_inherited_environment(self) -> None:
        runner = Runner(env={"KANBANLAN_TEST_ENV": "scoped"})
        with mock.patch("kanbanlan.runner.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            runner.run(["true"])

        environment = run.call_args.kwargs["env"]
        self.assertEqual("scoped", environment["KANBANLAN_TEST_ENV"])
        self.assertEqual(os.environ.get("PATH"), environment.get("PATH"))

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
