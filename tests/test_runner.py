from __future__ import annotations

import unittest
from unittest import mock

from kanbanlan.runner import CommandResult, Runner


class RunnerTests(unittest.TestCase):
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
