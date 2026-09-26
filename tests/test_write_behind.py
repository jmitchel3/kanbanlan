from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from kanbanlan import cli
from kanbanlan.config import Config
from kanbanlan.outbox import (
    APPLIED,
    FAILED,
    QUEUED,
    RUNNING,
    Execution,
    Intent,
    Outbox,
    drain,
    overlay,
    pending_item,
    write_behind_enabled,
)
from kanbanlan.snapshot import SCHEMA_VERSION, CacheStore, isoformat

REPOSITORY = "acme/widget"
ALPHA = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
BETA = "KBL-BBBBBBBBBBBBBBBBBBBBBBBBBB"


def config() -> Config:
    return Config(
        repository=REPOSITORY,
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def item(
    number: int,
    kanbanlan_id: str,
    status: str,
    *,
    claim: dict[str, Any] | None = None,
    pull_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "ISSUE",
        "kanbanlan_id": kanbanlan_id,
        "provider": "github",
        "provider_ref": f"github:{REPOSITORY}#{number}",
        "display_id": f"#{number}",
        "project_item_id": f"item-{number}",
        "number": number,
        "title": f"Request {number}",
        "repository": REPOSITORY,
        "state": "OPEN",
        "status": status,
        "priority": "priority:p2",
        "active_claim": claim,
        "session_history": [],
        "linked_open_pull_requests": pull_requests or [],
    }


def snapshot(items: list[dict[str, Any]], *, age_seconds: float = 0) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": isoformat(datetime.now(UTC) - timedelta(seconds=age_seconds)),
        "source": {"repository": REPOSITORY},
        "project": {"id": "project-1"},
        "items": items,
        "status_counts": {},
        "ready_cards": [],
        "next_ready": None,
        "open_pull_requests": [],
        "rate_limit": {},
    }


def intent(kind: str, kanbanlan_id: str | None, effect: dict[str, Any], **kwargs) -> Intent:
    value = Intent.create(
        kind=kind,
        reference=kanbanlan_id or "",
        label=kanbanlan_id or "",
        argv=[kind, kanbanlan_id or ""],
        effect=effect,
        kanbanlan_id=kanbanlan_id,
    )
    for name, field_value in kwargs.items():
        setattr(value, name, field_value)
    return value


class OverlayTests(unittest.TestCase):
    def test_a_queued_capture_appears_in_the_inbox_before_github_numbers_it(self) -> None:
        created = pending_item(
            kanbanlan_id=ALPHA,
            title="New",
            body="",
            priority="priority:p1",
            repository=REPOSITORY,
        )

        view = overlay(snapshot([]), [intent("capture", ALPHA, {"create": created})])

        (value,) = view["items"]
        self.assertEqual(("Inbox", None), (value["status"], value["number"]))
        self.assertEqual({"Inbox": 1}, view["status_counts"])

    def test_a_capture_is_not_duplicated_once_the_snapshot_has_it(self) -> None:
        created = pending_item(
            kanbanlan_id=ALPHA, title="New", body="", priority="priority:p2", repository=REPOSITORY
        )

        view = overlay(
            snapshot([item(7, ALPHA, "Inbox")]),
            [intent("capture", ALPHA, {"create": created}, state=APPLIED)],
        )

        self.assertEqual([7], [value["number"] for value in view["items"]])

    def test_a_queued_claim_takes_the_card_out_of_the_ready_queue(self) -> None:
        base = snapshot([item(7, ALPHA, "Ready"), item(8, BETA, "Ready")])
        claim = {"session": "claude:one", "branch": "work/a"}

        view = overlay(base, [intent("claim", ALPHA, {"status": "In progress", "claim": claim})])

        self.assertEqual(8, view["next_ready"]["number"])
        claimed = cli._issue(view, ALPHA)
        self.assertEqual(
            ("In progress", "claude:one"), (claimed["status"], claimed["active_claim"]["session"])
        )
        self.assertEqual({"In progress": 1, "Ready": 1}, view["status_counts"])

    def test_a_release_clears_the_claim(self) -> None:
        base = snapshot([item(7, ALPHA, "In progress", claim={"session": "claude:one"})])

        view = overlay(base, [intent("release", ALPHA, {"status": "Ready", "claim": None})])

        self.assertIsNone(cli._issue(view, ALPHA)["active_claim"])
        self.assertEqual(7, view["next_ready"]["number"])

    def test_failed_changes_never_overlay(self) -> None:
        base = snapshot([item(7, ALPHA, "Inbox")])

        view = overlay(base, [intent("triage", ALPHA, {"status": "Ready"}, state=FAILED)])

        self.assertEqual("Inbox", cli._issue(view, ALPHA)["status"])

    def test_the_snapshot_itself_is_never_modified(self) -> None:
        base = snapshot([item(7, ALPHA, "Inbox")])

        overlay(base, [intent("triage", ALPHA, {"status": "Ready"})])

        self.assertEqual("Inbox", base["items"][0]["status"])


class DrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.outbox = Outbox(Path(self.directory.name))
        self.executed: list[str] = []
        self.refreshes = 0

    def tearDown(self) -> None:
        self.directory.cleanup()

    def add(self, kind: str, kanbanlan_id: str | None, **kwargs) -> Intent:
        value = intent(kind, kanbanlan_id, {}, **kwargs)
        self.outbox.write(value)
        return value

    def run_drain(self, failing: set[str] | None = None, refresh_error: Exception | None = None):
        failing = failing or set()

        def execute(value: Intent) -> Execution:
            self.executed.append(value.id)
            if value.id in failing:
                return Execution(False, "claimed first by claude:other")
            return Execution(True)

        def refresh() -> None:
            self.refreshes += 1
            if refresh_error:
                raise refresh_error

        return drain(self.outbox, execute=execute, refresh=refresh)

    def test_changes_run_in_creation_order_and_are_removed_after_one_refresh(self) -> None:
        first = self.add("triage", ALPHA)
        second = self.add("claim", ALPHA)
        third = self.add("triage", BETA)

        self.assertTrue(self.run_drain())

        self.assertEqual([first.id, second.id, third.id], self.executed)
        self.assertEqual(1, self.refreshes)
        self.assertEqual([], self.outbox.intents())

    def test_a_failure_blocks_later_changes_to_the_same_request_only(self) -> None:
        first = self.add("claim", ALPHA)
        second = self.add("release", ALPHA)
        other = self.add("triage", BETA)

        self.run_drain(failing={first.id})

        self.assertEqual([first.id, other.id], self.executed)
        states = {value.id: (value.state, value.error) for value in self.outbox.intents()}
        self.assertEqual(FAILED, states[first.id][0])
        self.assertIn("claimed first", states[first.id][1])
        self.assertEqual(FAILED, states[second.id][0])
        self.assertIn("earlier claim", states[second.id][1])
        self.assertNotIn(other.id, states)

    def test_a_change_left_running_by_a_dead_drainer_is_failed_not_replayed(self) -> None:
        stranded = self.add("claim", ALPHA, state=RUNNING)

        self.run_drain()

        self.assertEqual([], self.executed)
        (value,) = self.outbox.intents()
        self.assertEqual((stranded.id, FAILED), (value.id, value.state))
        self.assertIn("unknown", value.error)

    def test_applied_changes_survive_a_failed_refresh(self) -> None:
        self.add("triage", ALPHA)

        with self.assertRaises(RuntimeError):
            self.run_drain(refresh_error=RuntimeError("network"))

        (value,) = self.outbox.intents()
        self.assertEqual(APPLIED, value.state)
        self.run_drain()
        self.assertEqual([], self.outbox.intents())

    def test_a_second_drainer_leaves_the_queue_to_the_first(self) -> None:
        self.add("triage", ALPHA)
        from kanbanlan.locks import FileLock

        self.outbox._prepare()
        with FileLock(self.outbox.drain_lock_path):
            self.assertFalse(self.run_drain())

        self.assertEqual([], self.executed)
        self.assertEqual(QUEUED, self.outbox.intents()[0].state)

    def test_a_change_queued_while_draining_is_picked_up(self) -> None:
        first = self.add("triage", ALPHA)
        late: list[Intent] = []

        def execute(value: Intent) -> Execution:
            self.executed.append(value.id)
            if not late:
                late.append(self.add("triage", BETA))
            return Execution(True)

        drain(self.outbox, execute=execute, refresh=lambda: None)

        self.assertEqual([first.id, late[0].id], self.executed)


class InstantCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = CacheStore(config(), self.root / "cache")
        self.provider = mock.Mock()
        self.provider.capabilities.project_scope = True
        self.started: list[Path] = []
        self.live_calls: list[str] = []
        patches = [
            mock.patch.dict(os.environ, {"KANBANLAN_WRITE_BEHIND": "1"}),
            mock.patch.object(
                cli, "_context", return_value=(self.root, config(), self.provider, self.store)
            ),
            mock.patch.object(cli, "_actor_session", return_value=None),
            mock.patch.object(cli, "_start_sync", side_effect=self.started.append),
            mock.patch.object(cli, "_spawn_refresh"),
            mock.patch.object(cli, "notify_if_update_available"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write_snapshot(self, items: list[dict[str, Any]], *, age_seconds: float = 0) -> None:
        self.store._prepare_directory()
        self.store._write_json(self.store.snapshot_path, snapshot(items, age_seconds=age_seconds))

    def run_cli(self, *argv: str) -> tuple[int, dict[str, Any] | None, str]:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["--json", *argv])
        text = stdout.getvalue() or stderr.getvalue()
        try:
            return code, json.loads(text), text
        except json.JSONDecodeError:
            return code, None, text

    @property
    def outbox(self) -> Outbox:
        return Outbox(self.store.directory)

    def test_write_behind_is_on_by_default_and_off_inside_the_drainer(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(write_behind_enabled())
        with mock.patch.dict(os.environ, {"KANBANLAN_SYNC_EXECUTOR": "1"}, clear=True):
            self.assertFalse(write_behind_enabled())

    def test_triage_queues_the_change_without_touching_github(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")])

        code, payload, _ = self.run_cli("triage", ALPHA)

        self.assertEqual(0, code)
        self.assertEqual("Ready", payload["result"]["status"])
        self.assertEqual("write-behind", payload["result"]["sync"]["mode"])
        (queued,) = self.outbox.intents()
        self.assertEqual(["triage", ALPHA], queued.argv)
        self.assertEqual([self.root], self.started)
        self.assertEqual([], self.provider.method_calls)

    def test_local_validation_rejects_an_impossible_transition(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Ready")])

        code, payload, _ = self.run_cli("triage", ALPHA)

        self.assertEqual(1, code)
        self.assertIn("not Inbox", payload["error"]["message"])
        self.assertEqual([], self.outbox.intents())

    def test_two_local_claims_on_one_card_are_arbitrated_instantly(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Ready")])
        with (
            mock.patch.object(
                cli, "_claim_checkout", side_effect=[("work/a", "/tmp/a"), ("work/b", "/tmp/b")]
            ),
            mock.patch.object(cli, "_create_worktree"),
            mock.patch.object(cli, "_fetch_is_stale", return_value=False),
        ):
            first, _, _ = self.run_cli("claim", ALPHA, "--touchpoints", "docs", "--session", "s1")
            second, payload, _ = self.run_cli(
                "claim", ALPHA, "--touchpoints", "docs", "--session", "s2"
            )

        self.assertEqual((0, 1), (first, second))
        self.assertIn("not Ready", payload["error"]["message"])
        (queued,) = self.outbox.intents()
        self.assertIn("--no-worktree", queued.argv)
        self.assertEqual("s1", queued.argv[queued.argv.index("--session") + 1])

    def test_a_request_with_a_failed_change_refuses_further_changes(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")])
        self.outbox.write(intent("claim", ALPHA, {}, state=FAILED, error="boom"))

        code, payload, _ = self.run_cli("triage", ALPHA)

        self.assertEqual(1, code)
        self.assertIn("failed to sync", payload["error"]["message"])

    def test_review_without_a_known_pull_request_runs_live_after_draining(self) -> None:
        self.write_snapshot([item(7, ALPHA, "In progress")])
        order: list[str] = []
        with (
            mock.patch.object(cli, "_drain_inline", side_effect=lambda *a: order.append("drain")),
            mock.patch.object(cli, "_review_live", side_effect=lambda a: order.append("live") or 0),
        ):
            code, _, _ = self.run_cli("review", ALPHA)

        self.assertEqual(0, code)
        self.assertEqual(["drain", "live"], order)
        self.assertEqual([], self.outbox.intents())

    def test_a_request_missing_from_the_snapshot_runs_live(self) -> None:
        self.write_snapshot([])
        with (
            mock.patch.object(cli, "_drain_inline"),
            mock.patch.object(cli, "_triage_live", return_value=0) as live,
        ):
            self.run_cli("triage", ALPHA)

        live.assert_called_once()

    def test_a_snapshot_too_old_to_trust_runs_live(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")], age_seconds=180 * 11)
        with (
            mock.patch.object(cli, "_drain_inline"),
            mock.patch.object(cli, "_triage_live", return_value=0) as live,
        ):
            self.run_cli("triage", ALPHA)

        live.assert_called_once()

    def test_capture_returns_its_kanbanlan_id_at_once(self) -> None:
        self.write_snapshot([])

        code, payload, _ = self.run_cli("capture", "New thing", "--priority", "priority:p1")

        self.assertEqual(0, code)
        kanbanlan_id = payload["result"]["kanbanlan_id"]
        self.assertTrue(kanbanlan_id.startswith("KBL-"))
        self.assertIsNone(payload["result"]["url"])
        (queued,) = self.outbox.intents()
        self.assertEqual(kanbanlan_id, queued.argv[queued.argv.index("--kanbanlan-id") + 1])
        # The queued request is immediately claimable by its ID.
        code, payload, _ = self.run_cli("triage", kanbanlan_id)
        self.assertEqual((0, "Ready"), (code, payload["result"]["status"]))

    def test_ensure_serves_a_stale_snapshot_and_refreshes_in_the_background(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")], age_seconds=400)

        code, payload, _ = self.run_cli("ensure")

        self.assertEqual(0, code)
        self.assertEqual("stale", payload["result"]["snapshot_state"])
        cli._spawn_refresh.assert_called_once_with(self.root)
        self.assertEqual([], self.provider.method_calls)

    def test_next_sees_queued_changes(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Ready"), item(8, BETA, "Inbox")])
        self.outbox.write(intent("triage", BETA, {"status": "Ready"}))
        self.outbox.write(
            intent("claim", ALPHA, {"status": "In progress", "claim": {"session": "x"}})
        )

        code, payload, _ = self.run_cli("next")

        self.assertEqual(8, payload["result"]["request"]["number"])

    def test_a_clean_cached_reconcile_answers_without_github(self) -> None:
        values = [item(7, ALPHA, "Inbox")]
        self.write_snapshot(values)
        self.store.write_open_requests([])
        with (
            mock.patch.object(cli, "plan_reconciliation", return_value=[]),
            mock.patch.object(cli, "_activate_worker"),
            mock.patch.object(cli, "read_board") as live,
        ):
            code, _, _ = self.run_cli("reconcile")

        self.assertEqual(0, code)
        live.assert_not_called()

    def test_cached_drift_is_confirmed_live_before_it_is_reported(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")])
        self.store.write_open_requests([])
        with (
            mock.patch.object(cli, "plan_reconciliation", side_effect=[["drift"], []]),
            mock.patch.object(cli, "_activate_worker"),
            mock.patch.object(cli, "read_board", return_value=(snapshot([]), [])) as live,
        ):
            code, _, _ = self.run_cli("reconcile")

        self.assertEqual(0, code)
        live.assert_called_once()

    def test_sync_retry_and_dismiss_manage_failed_changes(self) -> None:
        self.write_snapshot([])
        failed = intent("claim", ALPHA, {}, state=FAILED, error="boom")
        other = intent("triage", BETA, {}, state=FAILED, error="boom")
        self.outbox.write(failed)
        self.outbox.write(other)

        self.run_cli("sync", "--retry", failed.id)
        self.run_cli("sync", "--dismiss", other.id)

        (value,) = self.outbox.intents()
        self.assertEqual((failed.id, QUEUED, None), (value.id, value.state, value.error))


class ExecutorTests(unittest.TestCase):
    def test_the_live_commands_json_error_message_is_recorded(self) -> None:
        from kanbanlan.outbox import _executor_error

        stderr = json.dumps({"ok": False, "error": {"message": "claimed first by x"}})

        self.assertEqual("claimed first by x", _executor_error(stderr, ""))
        self.assertEqual("plain failure", _executor_error("noise\nplain failure\n", ""))

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
            self.assertEqual(0, cli._capture_live(args))

        provider.create_request.assert_not_called()
        self.assertEqual(ALPHA, json.loads(stdout.getvalue())["result"]["kanbanlan_id"])


class FailureDetailTests(unittest.TestCase):
    def test_an_indented_json_error_after_other_output_keeps_its_message(self) -> None:
        from kanbanlan.outbox import _executor_error

        error = {"ok": False, "error": {"kind": "RuntimeError", "message": "boom", "hint": "retry"}}
        stderr = "→ Creating issue\n" + json.dumps(error, indent=2, sort_keys=True) + "\n"

        self.assertEqual("boom (retry)", _executor_error(stderr, ""))

    def test_a_message_containing_braces_is_not_split(self) -> None:
        from kanbanlan.outbox import _executor_error

        stderr = json.dumps({"error": {"message": "bad {value}\n}"}}, indent=2)

        self.assertEqual("bad {value}\n}", _executor_error(stderr, ""))


class CaptureVisibilityTests(unittest.TestCase):
    def test_capture_waits_for_github_to_list_the_new_request(self) -> None:
        reads = iter([snapshot([]), snapshot([]), snapshot([item(74, ALPHA, "Inbox")])])
        with mock.patch.object(cli.time, "sleep") as sleep:
            value = cli._await_request(lambda: next(reads), lambda read: read, ALPHA)

        self.assertEqual(74, cli._issue(value, ALPHA)["number"])
        self.assertEqual([mock.call(1.0), mock.call(2.0)], sleep.call_args_list)

    def test_capture_stops_waiting_after_a_bounded_time(self) -> None:
        reads: list[int] = []

        def read() -> dict[str, Any]:
            reads.append(1)
            return snapshot([])

        with mock.patch.object(cli.time, "sleep") as sleep:
            cli._await_request(read, lambda value: value, ALPHA)

        self.assertEqual(len(cli.CAPTURE_VISIBILITY_DELAYS) + 1, len(reads))
        self.assertEqual(10.0, sum(call.args[0] for call in sleep.call_args_list))
