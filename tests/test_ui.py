from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kanbanlan.ui import configure_color, configure_progress, error, heading, status


class TerminalUiTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_color("auto")
        configure_progress(True)

    def test_color_can_be_forced_for_accessibility_testing(self) -> None:
        output = io.StringIO()
        configure_color("always")

        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            heading("Setup")

        self.assertIn("\033[1;36mSetup", output.getvalue())

    def test_no_color_environment_always_wins(self) -> None:
        output = io.StringIO()
        configure_color("always")

        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), redirect_stdout(output):
            heading("Setup")

        self.assertEqual("Setup\n", output.getvalue())

    def test_non_terminal_progress_has_start_and_completion_states(self) -> None:
        output = io.StringIO()

        with redirect_stderr(output), status("Loading Projects"):
            pass

        self.assertEqual("→ Loading Projects\n✓ Loading Projects\n", output.getvalue())

    def test_failed_progress_identifies_the_failed_step(self) -> None:
        output = io.StringIO()

        with redirect_stderr(output), self.assertRaises(RuntimeError):
            with status("Loading Projects"):
                raise RuntimeError("offline")

        self.assertEqual("→ Loading Projects\n✗ Loading Projects\n", output.getvalue())

    def test_progress_can_be_suppressed_for_structured_output(self) -> None:
        output = io.StringIO()
        configure_progress(False)

        with redirect_stderr(output), status("Loading Projects"):
            pass

        self.assertEqual("", output.getvalue())

    def test_error_visually_separates_remediation(self) -> None:
        output = io.StringIO()

        with redirect_stderr(output):
            error("authentication failed", hint="Run 'kanbanlan auth'.")

        self.assertEqual(
            "Error: authentication failed\nHint: Run 'kanbanlan auth'.\n",
            output.getvalue(),
        )
