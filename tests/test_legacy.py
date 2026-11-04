"""Migration paths for the removed background worker and write-behind outbox."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from board_fixtures import ALPHA, BETA, REPOSITORY, config, item, snapshot

from kanbanlan import cli, legacy
from kanbanlan.snapshot import CacheStore


def write_intent(directory: Path, seq: int, kind: str, state: str, **extra: Any) -> Path:
    outbox = directory / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    request = extra.pop("kanbanlan_id", ALPHA)
    value = {
        "id": f"{seq:012x}",
        "seq": seq,
        "kind": kind,
        "reference": request,
        "label": request,
        "argv": [kind, request],
        "effect": {},
        "kanbanlan_id": request,
        "state": state,
        **extra,
    }
    path = outbox / f"{seq:020d}-{value['id']}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class LegacyOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "cache"
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(legacy.SKIP_ENV, None)
        self.ran: list[list[str]] = []

    def execute(self, failing: set[str] | None = None) -> Any:
        def run(argv: list[str]) -> tuple[bool, str | None]:
            self.ran.append(argv)
            if argv[0] in (failing or set()):
                return False, f"{argv[0]} refused"
            return True, None

        return run

    def migrate(self, failing: set[str] | None = None) -> list[dict[str, Any]]:
        return legacy.apply_legacy_outbox(
            Path(self.directory.name), self.cache, execute=self.execute(failing)
        )

    def test_no_outbox_means_nothing_to_do(self) -> None:
        self.assertEqual([], self.migrate())
        self.assertEqual([], self.ran)

    def test_queued_changes_are_applied_in_order_once_and_the_outbox_removed(self) -> None:
        write_intent(self.cache, 2, "claim", "queued")
        write_intent(self.cache, 1, "triage", "waiting")
        write_intent(self.cache, 0, "capture", "applied")

        report = self.migrate()

        self.assertEqual([["triage", ALPHA], ["claim", ALPHA]], self.ran)
        self.assertEqual(["applied", "applied"], [entry["outcome"] for entry in report])
        self.assertFalse((self.cache / "outbox").exists())
        self.assertEqual([], self.migrate())
        self.assertEqual(2, len(self.ran))

    def test_changes_that_cannot_be_applied_are_reported_with_their_command(self) -> None:
        write_intent(self.cache, 1, "triage", "failed", error="not Inbox")
        write_intent(self.cache, 2, "claim", "queued")
        write_intent(self.cache, 3, "release", "running", kanbanlan_id=BETA)
        write_intent(self.cache, 4, "review", "queued", kanbanlan_id=BETA)
        (self.cache / "outbox" / "garbled.json").write_text("{", encoding="utf-8")

        report = self.migrate()

        # Nothing runs on top of a failed or interrupted change to its request.
        self.assertEqual([], self.ran)
        outcomes = {entry.get("kind") or "file": entry["outcome"] for entry in report}
        self.assertEqual(
            {
                "file": "unreadable",
                "triage": "not applied",
                "claim": "not applied",
                "release": "not applied",
                "review": "not applied",
            },
            outcomes,
        )
        lines = [legacy.describe_change(entry) for entry in report]
        self.assertTrue(any(f"run: kanbanlan claim {ALPHA}" in line for line in lines))
        self.assertTrue(any("not Inbox" in line for line in lines))
        self.assertFalse((self.cache / "outbox").exists())

    def test_a_failed_replay_blocks_later_changes_to_the_same_request_only(self) -> None:
        write_intent(self.cache, 1, "triage", "queued")
        write_intent(self.cache, 2, "claim", "queued")
        write_intent(self.cache, 3, "triage", "queued", kanbanlan_id=BETA)

        report = self.migrate(failing={"triage"})

        self.assertEqual([["triage", ALPHA], ["triage", BETA]], self.ran)
        self.assertEqual(
            ["failed", "not applied", "failed"], [entry["outcome"] for entry in report]
        )
        self.assertIn("triage refused", legacy.describe_change(report[0]))

    def test_the_replay_child_never_migrates_again(self) -> None:
        write_intent(self.cache, 1, "triage", "queued")
        with mock.patch.dict(os.environ, {legacy.SKIP_ENV: "1"}):
            self.assertEqual([], self.migrate())
        self.assertTrue((self.cache / "outbox").exists())

    def test_doctor_counts_what_is_still_queued(self) -> None:
        write_intent(self.cache, 1, "triage", "queued")
        write_intent(self.cache, 2, "claim", "applied")

        self.assertEqual(1, legacy.pending_legacy_changes(self.cache))

    def test_the_first_lifecycle_command_applies_the_outbox_and_reports_on_stderr(self) -> None:
        store = CacheStore(config(), self.cache)
        write_intent(self.cache, 1, "triage", "failed", error="boom")
        provider = mock.Mock()
        stdout, stderr = StringIO(), StringIO()
        with (
            mock.patch.object(
                cli,
                "_context",
                return_value=(Path(self.directory.name), config(), provider, store),
            ),
            mock.patch.object(cli, "_cmd_triage", return_value=0) as triage,
            mock.patch.object(cli, "notify_if_update_available"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(0, cli.main(["--json", "triage", ALPHA]))

        triage.assert_called_once()
        self.assertEqual("", stdout.getvalue())
        self.assertIn(f"kanbanlan triage {ALPHA}", stderr.getvalue())
        self.assertFalse((self.cache / "outbox").exists())

    def test_a_replay_runs_the_live_command_and_keeps_its_error(self) -> None:
        completed = mock.Mock(
            returncode=1,
            stdout="",
            stderr=json.dumps({"ok": False, "error": {"message": "claimed first by x"}}),
        )
        with mock.patch.object(legacy.subprocess, "run", return_value=completed) as run:
            ok, message = legacy._replay(Path("/repo"))(["claim", ALPHA])

        self.assertFalse(ok)
        self.assertEqual("claimed first by x", message)
        command = run.call_args.args[0]
        self.assertEqual(["-m", "kanbanlan", "-C", "/repo", "--json", "claim", ALPHA], command[1:])
        self.assertEqual("1", run.call_args.kwargs["env"][legacy.SKIP_ENV])


class WorkerUninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / "home"
        self.state = Path(self.directory.name) / "state"
        self.state.mkdir()
        patchers = [
            mock.patch.object(legacy.Path, "home", return_value=self.home),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self) -> tuple[Path, Path]:
        plist = legacy.launchd_unit()
        service = legacy.systemd_unit()
        for unit in (plist, service):
            unit.parent.mkdir(parents=True)
            unit.write_text("unit", encoding="utf-8")
        for name in ("registry.json", "worker.lock", "accounts.json"):
            (self.state / name).write_text("{}", encoding="utf-8")
        return plist, service

    def test_units_are_unloaded_and_removed_and_worker_state_deleted(self) -> None:
        plist, service = self.install()
        run = mock.Mock()

        result = legacy.uninstall_worker(run=run, directory=self.state)

        self.assertFalse(plist.exists())
        self.assertFalse(service.exists())
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(["launchctl", "bootout"], commands[0][:2])
        self.assertIn(["systemctl", "--user", "disable", "--now", legacy.SYSTEMD_UNIT], commands)
        self.assertEqual(sorted([str(plist), str(service)]), sorted(result["units_removed"]))
        self.assertFalse((self.state / "registry.json").exists())
        self.assertFalse((self.state / "worker.lock").exists())
        # Account bindings are not worker state.
        self.assertTrue((self.state / "accounts.json").exists())

    def test_a_missing_service_manager_still_removes_the_unit(self) -> None:
        plist, _ = self.install()

        legacy.uninstall_worker(run=mock.Mock(side_effect=FileNotFoundError), directory=self.state)

        self.assertFalse(plist.exists())

    def test_a_running_worker_is_stopped(self) -> None:
        with (
            mock.patch.object(legacy, "running_worker", return_value=4242),
            mock.patch.object(legacy.os, "kill") as kill,
            mock.patch.object(legacy, "pid_running", return_value=False),
        ):
            result = legacy.uninstall_worker(run=mock.Mock(), directory=self.state)

        kill.assert_called_once_with(4242, legacy.signal.SIGTERM)
        self.assertEqual(4242, result["stopped_pid"])

    def test_nothing_installed_is_reported_as_such(self) -> None:
        self.assertEqual(
            {"units": [], "pid": None, "state_files": []},
            legacy.worker_leftovers(self.state),
        )
        stdout = StringIO()
        with (
            mock.patch.object(
                cli,
                "uninstall_worker",
                return_value=legacy.uninstall_worker(run=mock.Mock(), directory=self.state),
            ),
            mock.patch.object(cli, "notify_if_update_available"),
            redirect_stdout(stdout),
        ):
            self.assertEqual(0, cli.main(["admin", "worker", "uninstall"]))
        self.assertIn("No background worker is installed", stdout.getvalue())

    def test_leftovers_name_installed_units(self) -> None:
        plist, service = self.install()

        leftovers = legacy.worker_leftovers(self.state)

        self.assertEqual(sorted([str(plist), str(service)]), sorted(leftovers["units"]))
        self.assertEqual(2, len(leftovers["state_files"]))


class ReplayedCaptureTests(unittest.TestCase):
    def test_a_replayed_capture_with_an_existing_id_opens_no_second_issue(self) -> None:
        provider = mock.Mock()
        existing = item(7, ALPHA, "Inbox")
        args = cli.build_parser().parse_args(
            ["--json", "capture", "Title", "--kanbanlan-id", ALPHA]
        )
        with (
            mock.patch.object(
                cli, "_context", return_value=(Path("/tmp"), config(), provider, mock.Mock())
            ),
            mock.patch.object(cli, "_actor_session", return_value=None),
            mock.patch.object(cli, "_capture_target", return_value=(REPOSITORY, None)),
            mock.patch.object(cli, "_existing_request", return_value=existing),
            redirect_stdout(StringIO()) as stdout,
        ):
            self.assertEqual(0, cli._cmd_capture(args))

        provider.create_request.assert_not_called()
        self.assertEqual(ALPHA, json.loads(stdout.getvalue())["result"]["kanbanlan_id"])


class RateLimitKindTests(unittest.TestCase):
    def test_a_rate_limited_gh_command_is_reported_as_a_rate_limit_error(self) -> None:
        from kanbanlan.runner import CommandError, CommandResult, RateLimitError

        refused = CommandError(
            CommandResult(("gh", "api"), 1, "", "gh: API rate limit exceeded (HTTP 403)")
        )
        stderr = StringIO()
        with (
            mock.patch.object(cli, "_cmd_status", side_effect=refused),
            mock.patch.object(cli, "notify_if_update_available"),
            redirect_stderr(stderr),
        ):
            self.assertEqual(1, cli.main(["--json", "status"]))
        self.assertEqual("RateLimitError", json.loads(stderr.getvalue())["error"]["kind"])

        limited = RateLimitError("limit", reset_at="2026-09-29T12:00:00Z")
        stderr = StringIO()
        with (
            mock.patch.object(cli, "_cmd_status", side_effect=limited),
            mock.patch.object(cli, "notify_if_update_available"),
            redirect_stderr(stderr),
        ):
            cli.main(["--json", "status"])
        self.assertEqual("2026-09-29T12:00:00Z", json.loads(stderr.getvalue())["error"]["reset_at"])


class ReplayLookupTests(unittest.TestCase):
    def lookup(self, cached: dict[str, Any] | None, found: dict[str, Any] | None = None):
        provider = mock.Mock()
        provider.find_request.return_value = found
        store = mock.Mock()
        store.snapshot.return_value = cached
        value = cli._existing_request(provider, store, ALPHA, REPOSITORY, config())
        store.refresh.assert_not_called()
        provider.snapshot.assert_not_called()
        return value, provider

    def test_a_request_on_the_cached_board_needs_no_github_read(self) -> None:
        value, provider = self.lookup(snapshot([item(74, ALPHA, "Inbox")]))

        self.assertEqual(74, value["number"])
        provider.find_request.assert_not_called()

    def test_a_request_missing_from_the_cache_is_found_by_one_search(self) -> None:
        found = {"number": 75, "kanbanlan_id": ALPHA}
        value, provider = self.lookup(snapshot([]), found)

        self.assertEqual(found, value)
        provider.find_request.assert_called_once_with(ALPHA, repository=REPOSITORY)

    def test_no_cache_and_no_match_means_the_capture_runs(self) -> None:
        value, _ = self.lookup(None, None)

        self.assertIsNone(value)


class ReplayRepairTests(unittest.TestCase):
    def replay(self, existing: dict[str, Any]) -> mock.Mock:
        provider = mock.Mock()
        provider.add_to_projection.return_value = {"id": "PVTI_7"}
        store = mock.Mock()
        store.snapshot.return_value = None
        args = cli.build_parser().parse_args(
            ["--json", "capture", "Title", "--kanbanlan-id", ALPHA]
        )
        with (
            mock.patch.object(
                cli, "_context", return_value=(Path("/tmp"), config(), provider, store)
            ),
            mock.patch.object(cli, "_actor_session", return_value=None),
            mock.patch.object(cli, "_capture_target", return_value=(REPOSITORY, None)),
            mock.patch.object(cli, "_existing_request", return_value=existing),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(0, cli._cmd_capture(args))
        provider.create_request.assert_not_called()
        return provider

    def found(self, label: str, state: str = "OPEN") -> dict[str, Any]:
        return {
            "kanbanlan_id": ALPHA,
            "number": 7,
            "url": f"https://github.test/{REPOSITORY}/issues/7",
            "state": state,
            "labels": [{"name": label}],
        }

    def test_a_replay_places_an_issue_the_lost_attempt_left_off_the_board(self) -> None:
        provider = self.replay(self.found("status:intake"))

        provider.add_to_projection.assert_called_once_with(
            f"https://github.test/{REPOSITORY}/issues/7"
        )
        self.assertEqual("Inbox", provider.set_projection_status.call_args.args[2])

    def test_a_replay_never_moves_a_request_that_already_progressed(self) -> None:
        provider = self.replay(self.found("status:in-progress"))

        provider.add_to_projection.assert_not_called()
        provider.set_projection_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
