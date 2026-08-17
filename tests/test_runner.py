from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from kanbanlan.runner import (
    RETRY_ATTEMPTS,
    CommandError,
    CommandResult,
    Runner,
    is_transient_failure,
)


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


OUTAGE = "gh: No server is currently available to service your request. (HTTP 503)"


class RunnerRetryTests(unittest.TestCase):
    def test_transient_failures_are_recognized(self) -> None:
        for stderr in (
            OUTAGE,
            "gh: Something failed (HTTP 502)",
            "504 Gateway Timeout",
            "503 Service Unavailable",
        ):
            with self.subTest(stderr=stderr):
                self.assertTrue(is_transient_failure(CommandResult(("gh",), 1, "", stderr)))

    def test_successful_and_ordinary_failures_are_not_transient(self) -> None:
        # A zero exit never retries, even when the body mentions an outage.
        self.assertFalse(is_transient_failure(CommandResult(("gh",), 0, OUTAGE, "")))
        self.assertFalse(
            is_transient_failure(CommandResult(("gh",), 1, "", "could not resolve to a Repository"))
        )

    def test_transient_failure_is_retried_until_it_succeeds(self) -> None:
        runner = Runner()
        attempts = [
            mock.Mock(returncode=1, stdout="", stderr=OUTAGE),
            mock.Mock(returncode=0, stdout="{}", stderr=""),
        ]
        with (
            mock.patch("kanbanlan.runner.subprocess.run", side_effect=attempts) as run,
            mock.patch("kanbanlan.runner.time.sleep") as sleep,
        ):
            result = runner.run(["gh", "api"], retry=True)

        self.assertEqual(0, result.returncode)
        self.assertEqual(2, run.call_count)
        self.assertEqual([mock.call(1.0)], sleep.call_args_list)

    def test_retries_are_bounded_and_still_raise(self) -> None:
        runner = Runner()
        outage = mock.Mock(returncode=1, stdout="", stderr=OUTAGE)
        with (
            mock.patch("kanbanlan.runner.subprocess.run", return_value=outage) as run,
            mock.patch("kanbanlan.runner.time.sleep") as sleep,
            self.assertRaises(CommandError) as raised,
        ):
            runner.run(["gh", "api"], retry=True)

        self.assertEqual(RETRY_ATTEMPTS, run.call_count)
        self.assertIn("No server is currently available", str(raised.exception))
        # Backoff grows, and the run does not sleep after the final attempt.
        self.assertEqual([mock.call(1.0), mock.call(2.0)], sleep.call_args_list)

    def test_retry_is_opt_in(self) -> None:
        runner = Runner()
        outage = mock.Mock(returncode=1, stdout="", stderr=OUTAGE)
        with (
            mock.patch("kanbanlan.runner.subprocess.run", return_value=outage) as run,
            mock.patch("kanbanlan.runner.time.sleep") as sleep,
            self.assertRaises(CommandError),
        ):
            runner.run(["gh", "api"])

        self.assertEqual(1, run.call_count)
        sleep.assert_not_called()

    def test_ordinary_failure_is_not_retried(self) -> None:
        runner = Runner()
        denied = mock.Mock(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
        with (
            mock.patch("kanbanlan.runner.subprocess.run", return_value=denied) as run,
            mock.patch("kanbanlan.runner.time.sleep"),
            self.assertRaises(CommandError),
        ):
            runner.run(["gh", "api"], retry=True)

        self.assertEqual(1, run.call_count)

    def test_unchecked_transient_failure_is_retried_without_raising(self) -> None:
        runner = Runner()
        attempts = [
            mock.Mock(returncode=1, stdout="", stderr=OUTAGE),
            mock.Mock(returncode=1, stdout="", stderr="missing project scope"),
        ]
        with (
            mock.patch("kanbanlan.runner.subprocess.run", side_effect=attempts) as run,
            mock.patch("kanbanlan.runner.time.sleep"),
        ):
            result = runner.run(["gh", "project", "list"], check=False, retry=True)

        self.assertEqual(2, run.call_count)
        self.assertEqual("missing project scope", result.stderr)

    def test_json_passes_retry_through(self) -> None:
        runner = Runner()
        attempts = [
            mock.Mock(returncode=1, stdout="", stderr=OUTAGE),
            mock.Mock(returncode=0, stdout='{"ok": true}', stderr=""),
        ]
        with (
            mock.patch("kanbanlan.runner.subprocess.run", side_effect=attempts) as run,
            mock.patch("kanbanlan.runner.time.sleep"),
        ):
            self.assertEqual({"ok": True}, runner.json(["gh", "api"], retry=True))

        self.assertEqual(2, run.call_count)
