from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from kanbanlan.updates import CHECK_INTERVAL_SECONDS, _is_newer_release, notify_if_update_available


class UpdateCheckTests(unittest.TestCase):
    def test_new_release_is_reported_once_per_interval(self) -> None:
        response = io.BytesIO(json.dumps({"info": {"version": "0.3.0"}}).encode())
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "version-check.json"
            with (
                mock.patch("kanbanlan.updates.urlopen", return_value=response) as urlopen,
                redirect_stderr(stderr),
            ):
                notify_if_update_available("0.2.0", state_path=state_path, now=1000)
                notify_if_update_available("0.2.0", state_path=state_path, now=1001)

        urlopen.assert_called_once()
        self.assertIn("Kanbanlan 0.3.0 is available", stderr.getvalue())
        self.assertIn("kanbanlan upgrade", stderr.getvalue())

    def test_release_is_checked_again_after_interval(self) -> None:
        responses = [
            io.BytesIO(json.dumps({"info": {"version": "0.2.0"}}).encode()),
            io.BytesIO(json.dumps({"info": {"version": "0.2.0"}}).encode()),
        ]
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "version-check.json"
            with mock.patch("kanbanlan.updates.urlopen", side_effect=responses) as urlopen:
                notify_if_update_available("0.2.0", state_path=state_path, now=1000)
                notify_if_update_available(
                    "0.2.0",
                    state_path=state_path,
                    now=1000 + CHECK_INTERVAL_SECONDS,
                )

        self.assertEqual(2, urlopen.call_count)

    def test_failed_check_is_silent_and_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "version-check.json"
            with mock.patch("kanbanlan.updates.urlopen", side_effect=OSError) as urlopen:
                notify_if_update_available("0.2.0", state_path=state_path, now=1000)
                notify_if_update_available("0.2.0", state_path=state_path, now=1001)

        urlopen.assert_called_once()

    def test_version_comparison_handles_different_release_widths(self) -> None:
        self.assertTrue(_is_newer_release("0.10.0", "0.2.0"))
        self.assertFalse(_is_newer_release("0.2", "0.2.0"))
        self.assertFalse(_is_newer_release("invalid", "0.2.0"))
