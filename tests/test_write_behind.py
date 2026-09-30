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
    WAITING,
    Execution,
    Intent,
    Outbox,
    drain,
    overlay,
    pending_item,
    settle,
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

    def test_a_change_waiting_for_quota_still_overlays(self) -> None:
        base = snapshot([item(7, ALPHA, "Inbox")])

        view = overlay(base, [intent("triage", ALPHA, {"status": "Ready"}, state=WAITING)])

        self.assertEqual("Ready", cli._issue(view, ALPHA)["status"])

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

        def refresh() -> dict[str, Any]:
            self.refreshes += 1
            if refresh_error:
                raise refresh_error
            return snapshot([])

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

    def test_applied_changes_stay_until_a_snapshot_read_after_them_lands(self) -> None:
        self.add("triage", ALPHA)

        drain(self.outbox, execute=lambda value: Execution(True), refresh=lambda: None)
        (value,) = self.outbox.intents()
        self.assertEqual(APPLIED, value.state)
        settle(self.outbox, snapshot([], age_seconds=60))
        self.assertEqual([value.id], [other.id for other in self.outbox.intents()])

        settle(self.outbox, snapshot([]))

        self.assertEqual([], self.outbox.intents())


class QuotaWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.outbox = Outbox(Path(self.directory.name))
        self.executed: list[str] = []
        self.limited: set[str] = set()
        self.reset_at: str | None = None

    def tearDown(self) -> None:
        self.directory.cleanup()

    def add(self, kind: str, kanbanlan_id: str | None, **kwargs) -> Intent:
        value = intent(kind, kanbanlan_id, {}, **kwargs)
        self.outbox.write(value)
        return value

    def execute(self, value: Intent) -> Execution:
        self.executed.append(value.id)
        if value.id in self.limited:
            return Execution(
                False,
                "API rate limit exceeded",
                rate_limited=True,
                retry_at=self.reset_at,
            )
        return Execution(True)

    def run_drain(self) -> bool:
        return drain(self.outbox, execute=self.execute, refresh=lambda: snapshot([]))

    def states(self) -> dict[str, Intent]:
        return {value.id: value for value in self.outbox.intents()}

    def test_a_quota_refusal_waits_with_its_reset_and_holds_later_changes(self) -> None:
        first = self.add("claim", ALPHA)
        second = self.add("release", ALPHA)
        other = self.add("triage", BETA)
        self.limited = {first.id}
        self.reset_at = isoformat(datetime.now(UTC) + timedelta(minutes=20))

        self.run_drain()

        self.assertEqual([first.id], self.executed)
        states = self.states()
        self.assertEqual(
            (WAITING, self.reset_at), (states[first.id].state, states[first.id].retry_at)
        )
        self.assertEqual(QUEUED, states[second.id].state)
        self.assertEqual(QUEUED, states[other.id].state)
        self.assertEqual([], self.outbox.failed())

    def test_a_drain_before_the_reset_runs_nothing(self) -> None:
        self.add(
            "claim",
            ALPHA,
            state=WAITING,
            retry_at=isoformat(datetime.now(UTC) + timedelta(minutes=5)),
        )
        self.add("triage", BETA)

        self.run_drain()

        self.assertEqual([], self.executed)
        self.assertFalse(self.outbox.has_work())

    def test_the_next_drain_after_the_reset_requeues_it_in_order(self) -> None:
        past = isoformat(datetime.now(UTC) - timedelta(seconds=1))
        first = self.add("claim", ALPHA, state=WAITING, retry_at=past, error="rate limited")
        second = self.add("release", ALPHA)
        other = self.add("triage", BETA)
        self.assertTrue(self.outbox.has_work())

        self.run_drain()

        self.assertEqual([first.id, second.id, other.id], self.executed)
        self.assertEqual([], self.outbox.intents())

    def test_a_refusal_without_a_reset_time_waits_briefly(self) -> None:
        first = self.add("claim", ALPHA)
        self.limited = {first.id}

        self.run_drain()

        retry_at = self.states()[first.id].retry_at
        assert retry_at is not None
        delay = datetime.fromisoformat(retry_at.replace("Z", "+00:00")) - datetime.now(UTC)
        self.assertGreater(delay, timedelta(seconds=30))
        self.assertLessEqual(delay, timedelta(seconds=60))

    def test_a_reset_already_past_still_waits_briefly(self) -> None:
        first = self.add("claim", ALPHA)
        self.limited = {first.id}
        self.reset_at = isoformat(datetime.now(UTC) - timedelta(minutes=1))

        self.run_drain()

        self.assertEqual([first.id], self.executed)
        self.assertEqual(WAITING, self.states()[first.id].state)

    def test_other_failures_stay_terminal(self) -> None:
        first = self.add("claim", ALPHA)

        drain(
            self.outbox,
            execute=lambda value: Execution(False, "claimed first by claude:other"),
            refresh=lambda: None,
        )

        self.assertEqual(FAILED, self.states()[first.id].state)
        self.assertIsNone(self.states()[first.id].retry_at)


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

    def card(self, value: dict[str, Any]) -> dict[str, Any]:
        return {**snapshot([value]), "items": [value]}

    def test_review_reads_its_one_card_for_a_pull_request_opened_since_the_snapshot(
        self,
    ) -> None:
        self.write_snapshot([item(7, ALPHA, "In progress")])
        pull_request = {
            "provider_ref": f"github:{REPOSITORY}#11",
            "repository": REPOSITORY,
            "url": "https://github.test/pull/11",
            "linked_by": ["closing_reference"],
        }
        self.provider.read_request.return_value = self.card(
            item(7, ALPHA, "In progress", pull_requests=[pull_request])
        )

        code, payload, _ = self.run_cli("review", ALPHA)

        self.assertEqual(0, code)
        self.assertEqual("In review", payload["result"]["status"])
        self.assertTrue(self.provider.read_request.call_args.kwargs["pull_requests"])
        (queued,) = self.outbox.intents()
        self.assertEqual("review", queued.kind)

    def test_review_with_no_pull_request_on_the_live_card_is_refused(self) -> None:
        self.write_snapshot([item(7, ALPHA, "In progress")])
        self.provider.read_request.return_value = self.card(item(7, ALPHA, "In progress"))

        code, payload, _ = self.run_cli("review", ALPHA)

        self.assertEqual(1, code)
        self.assertIn("no linked open pull request", payload["error"]["message"])
        self.assertEqual([], self.outbox.intents())

    def test_a_request_newer_than_the_snapshot_is_read_as_one_card(self) -> None:
        self.write_snapshot([])
        self.provider.find_request.return_value = {"number": 7, "repository": REPOSITORY}
        self.provider.read_request.return_value = self.card(item(7, ALPHA, "Inbox"))

        code, payload, _ = self.run_cli("triage", ALPHA)

        self.assertEqual(0, code)
        self.assertEqual("Ready", payload["result"]["status"])
        self.provider.read_request.assert_called_once()
        self.assertEqual(7, self.provider.read_request.call_args.args[0])
        self.provider.snapshot.assert_not_called()

    def test_a_snapshot_past_its_serving_window_is_checked_against_the_card(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")], age_seconds=180 * 11)
        self.provider.read_request.return_value = self.card(item(7, ALPHA, "Ready"))

        code, payload, _ = self.run_cli("triage", ALPHA)

        self.assertEqual(1, code)
        self.assertIn("not Inbox", payload["error"]["message"])
        self.provider.find_request.assert_not_called()

    def test_an_unreadable_card_falls_back_to_the_stale_snapshot(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")], age_seconds=180 * 11)
        self.provider.read_request.side_effect = RuntimeError("GitHub unavailable")

        code, payload, _ = self.run_cli("triage", ALPHA)

        self.assertEqual(0, code)
        self.assertEqual("Ready", payload["result"]["status"])

    def test_no_snapshot_and_no_github_is_an_error_not_a_wait(self) -> None:
        self.provider.find_request.side_effect = RuntimeError("GitHub unavailable")

        code, payload, _ = self.run_cli("triage", ALPHA)

        self.assertEqual(1, code)
        self.assertIn("GitHub unavailable", payload["error"]["message"])
        self.assertEqual([], self.outbox.intents())

    def test_claim_without_a_worktree_queues_too(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Ready")])
        with mock.patch.object(cli, "_claim_checkout", return_value=("work/a", "/tmp/a")):
            code, payload, _ = self.run_cli(
                "claim", ALPHA, "--touchpoints", "docs", "--session", "s1", "--no-worktree"
            )

        self.assertEqual(0, code)
        self.assertEqual("write-behind", payload["result"]["sync"]["mode"])
        self.assertEqual([], self.provider.method_calls)

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

    def test_sync_shows_a_change_waiting_for_quota(self) -> None:
        self.write_snapshot([])
        reset = isoformat(datetime.now(UTC) + timedelta(minutes=10))
        self.outbox.write(intent("claim", ALPHA, {}, state=WAITING, retry_at=reset, error="limit"))
        stdout = StringIO()

        with redirect_stdout(stdout):
            code = cli.main(["sync"])

        self.assertEqual(0, code)
        self.assertIn(f"waiting for GitHub quota until {reset}", stdout.getvalue())
        self.assertEqual([], self.started)

    def test_plain_sync_starts_a_drainer_once_the_reset_passed(self) -> None:
        self.write_snapshot([])
        past = isoformat(datetime.now(UTC) - timedelta(seconds=5))
        self.outbox.write(intent("claim", ALPHA, {}, state=WAITING, retry_at=past))

        code, payload, _ = self.run_cli("sync")

        self.assertEqual(0, code)
        self.assertEqual([self.root], self.started)
        assert payload is not None
        self.assertEqual(WAITING, payload["result"]["changes"][0]["state"])

    def test_a_waiting_change_does_not_block_new_changes_to_its_request(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")])
        reset = isoformat(datetime.now(UTC) + timedelta(minutes=10))
        self.outbox.write(intent("claim", ALPHA, {}, state=WAITING, retry_at=reset))

        code, _, text = self.run_cli("triage", ALPHA)

        self.assertEqual(0, code, text)
        self.assertEqual(2, len(self.outbox.intents()))


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


class QuotaFailureDetailTests(unittest.TestCase):
    def failure(self, error: dict[str, Any]) -> Execution:
        from kanbanlan.outbox import _executor_failure

        return _executor_failure(json.dumps({"ok": False, "error": error}, indent=2), "")

    def test_a_rate_limit_error_kind_carries_its_reset_time(self) -> None:
        result = self.failure(
            {"kind": "RateLimitError", "message": "limit", "reset_at": "2026-09-29T12:00:00Z"}
        )

        self.assertEqual((True, "2026-09-29T12:00:00Z"), (result.rate_limited, result.retry_at))

    def test_the_deferral_message_yields_its_reset_time(self) -> None:
        result = self.failure(
            {
                "kind": "RuntimeError",
                "message": "GitHub refresh deferred until 2026-09-29T12:00:00Z to preserve quota",
            }
        )

        self.assertEqual((True, "2026-09-29T12:00:00Z"), (result.rate_limited, result.retry_at))

    def test_a_message_quoting_rate_limit_words_is_not_a_quota_refusal(self) -> None:
        result = self.failure(
            {"kind": "RuntimeError", "message": "claim of 'API rate limit exceeded' failed"}
        )

        self.assertFalse(result.rate_limited)

    def test_plain_output_is_never_a_quota_refusal(self) -> None:
        from kanbanlan.outbox import _executor_failure

        result = _executor_failure("gh: API rate limit exceeded\n", "")

        self.assertFalse(result.rate_limited)

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
            self.assertEqual(0, cli._capture_live(args))
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
