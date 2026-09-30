"""Lifecycle commands never read the whole board or wait on the refresh lock.

A full read of a large Project takes minutes and serializes every session
on one lock, so neither a lifecycle command nor the drainer's replay of it
may start one. Each test below runs a command against a fake GitHub that
holds one card and fails the moment anything reads the board, refreshes the
shared snapshot, or takes ``refresh.lock``.
"""

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

from kanbanlan import cli, locks, outbox
from kanbanlan.config import Config
from kanbanlan.identity import attach_kanbanlan_id
from kanbanlan.outbox import QUEUED, Intent, Outbox, drain_outbox
from kanbanlan.runner import RateLimitError
from kanbanlan.snapshot import SCHEMA_VERSION, CacheStore, build_snapshot, isoformat

REPOSITORY = "acme/widget"
IDENTITY = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"


class BoardRead(AssertionError):
    """Something read the whole board or took the refresh lock."""


def config() -> Config:
    return Config(
        repository=REPOSITORY,
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


class OneCardGitHub:
    """A GitHub that holds one request and refuses any board-wide read."""

    provider_name = "github"

    def __init__(
        self,
        status: str,
        *,
        claimed_by: str | None = None,
        pull_request: bool = False,
        on_board: bool = True,
    ):
        from kanbanlan.providers import ProviderCapabilities

        self.capabilities = ProviderCapabilities()
        self.on_board = on_board
        self.hidden_prefix: str | None = None
        self.status = status
        self.state = "OPEN"
        self.comments: list[dict[str, Any]] = []
        self.pull_request = pull_request
        self.calls: list[str] = []
        if claimed_by:
            self.comment_request(7, f"CLAIM: 2026-09-29T00:00:00Z\nSession: {claimed_by}")
            self.calls.clear()

    # Board-wide reads: any call fails the test.
    def snapshot(self, **_kwargs: Any) -> dict[str, Any]:
        raise BoardRead("provider.snapshot read the whole board")

    def fetch(self) -> Any:
        raise BoardRead("provider.fetch read the whole board")

    def collect(self, **_kwargs: Any) -> Any:
        raise BoardRead("provider.collect read the whole board")

    def list_open_requests(self) -> list[dict[str, Any]]:
        raise BoardRead("provider.list_open_requests listed every open request")

    # Targeted reads and writes for the one card.
    def read_request(self, reference: int | str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("read_request")
        pull_requests = []
        if self.pull_request and kwargs.get("pull_requests"):
            pull_requests.append(
                {
                    "number": 11,
                    "title": "Delivery",
                    "body": "",
                    "url": f"https://github.test/{REPOSITORY}/pull/11",
                    "repository": {"nameWithOwner": REPOSITORY},
                    "closingIssuesReferences": {
                        "nodes": [{"number": 7, "repository": {"nameWithOwner": REPOSITORY}}]
                    },
                }
            )
        return build_snapshot(config(), self._project(), pull_requests, {})

    def find_request(self, kanbanlan_id: str, **_kwargs: Any) -> dict[str, Any] | None:
        self.calls.append("find_request")
        return {"number": 7, "repository": REPOSITORY, "kanbanlan_id": kanbanlan_id}

    def projection_metadata(self) -> dict[str, Any]:
        self.calls.append("projection_metadata")
        return {"id": "project-1", "fields": {"nodes": []}}

    def set_projection_status(self, item_id: str, projection: Any, status: str) -> None:
        self.calls.append(f"status:{status}")
        self.status = status

    def set_request_status(self, reference: Any, label: Any, **_kwargs: Any) -> None:
        self.calls.append(f"label:{label}")

    def comment_request(self, reference: Any, body: str, **_kwargs: Any) -> None:
        self.calls.append("comment")
        created = datetime(2026, 9, 29, tzinfo=UTC) + timedelta(seconds=len(self.comments))
        self.comments.append(
            {"body": body, "createdAt": isoformat(created), "author": {"login": "agent"}}
        )

    def close_request(self, reference: Any, **_kwargs: Any) -> None:
        self.calls.append("close")
        self.state = "CLOSED"

    def create_request(self, *_args: Any, **_kwargs: Any) -> str:
        self.calls.append("create")
        return f"https://github.test/{REPOSITORY}/issues/7"

    def add_to_projection(self, url: str) -> dict[str, Any]:
        self.calls.append("add")
        return {"id": "item-7"}

    def _project(self) -> dict[str, Any]:
        return {
            "id": "project-1",
            "items": [
                {
                    "id": "item-7" if self.on_board else None,
                    "type": "ISSUE",
                    "isArchived": False,
                    "fieldValues": {
                        "nodes": (
                            [{"name": self.status, "field": {"name": "Status"}}]
                            if self.on_board
                            else []
                        )
                    },
                    "content": {
                        "id": "issue-7",
                        "number": 7,
                        "title": "Request 7",
                        "body": attach_kanbanlan_id("Body", IDENTITY),
                        "url": f"https://github.test/{REPOSITORY}/issues/7",
                        "state": self.state,
                        "repository": {"nameWithOwner": REPOSITORY},
                        "labels": {"nodes": [{"name": "priority:p2", "color": ""}]},
                        "assignees": {"nodes": []},
                        "comments": {
                            "nodes": [
                                value
                                for value in self.comments
                                if not (
                                    self.hidden_prefix
                                    and value["body"].startswith(self.hidden_prefix)
                                )
                            ]
                        },
                    },
                }
            ],
        }


def cached_snapshot(github: OneCardGitHub, *, age_seconds: float) -> dict[str, Any]:
    value = build_snapshot(config(), github._project(), [], {})
    value["generated_at"] = isoformat(datetime.now(UTC) - timedelta(seconds=age_seconds))
    value["schema_version"] = SCHEMA_VERSION
    return value


def guard_refresh_lock(*names: str) -> Any:
    """Fail on entering ``refresh.lock``, or any other lock file named."""

    enter = locks.FileLock.__enter__
    refused = {"refresh.lock", *names}

    def guarded(self: locks.FileLock) -> locks.FileLock:
        if Path(self.path).name in refused:
            raise BoardRead(f"took {self.path}")
        return enter(self)

    return mock.patch.object(locks.FileLock, "__enter__", guarded)


# Each lifecycle command as a session types it, and the card state it needs.
COMMANDS: list[tuple[str, list[str], dict[str, Any]]] = [
    ("triage", ["triage", IDENTITY], {"status": "Inbox"}),
    (
        "claim",
        ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--branch", "work/a"],
        {"status": "Ready"},
    ),
    (
        "claim --no-worktree",
        ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
        {"status": "Ready"},
    ),
    ("release", ["release", IDENTITY, "--reason", "done"], {"status": "In progress", "claim": 1}),
    (
        "release --blocked",
        ["release", IDENTITY, "--reason", "waiting", "--blocked"],
        {"status": "In progress", "claim": 1},
    ),
    ("review", ["review", IDENTITY], {"status": "In progress", "pull_request": True}),
    ("close", ["close", IDENTITY, "--reason", "shipped"], {"status": "In progress"}),
    (
        "handoff",
        [
            "handoff",
            IDENTITY,
            "--session",
            "s2",
            "--branch",
            "work/a",
            "--worktree",
            "/tmp/a",
            "--reason",
            "shift change",
        ],
        {"status": "In progress", "claim": 1},
    ),
]


class NoBoardReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def github_for(self, needs: dict[str, Any]) -> OneCardGitHub:
        return OneCardGitHub(
            needs["status"],
            claimed_by="s1" if needs.get("claim") else None,
            pull_request=bool(needs.get("pull_request")),
        )

    def run_command(
        self,
        argv: list[str],
        github: OneCardGitHub,
        *,
        environment: dict[str, str],
        snapshot: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        store = CacheStore(config(), self.root / "cache")
        if snapshot is not None:
            store._write_json(store.snapshot_path, snapshot)

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise BoardRead("refreshed the shared snapshot")

        stdout, stderr = StringIO(), StringIO()
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(cli, "_context", return_value=(self.root, config(), github, store)),
            mock.patch.object(cli, "_actor_session", return_value=None),
            mock.patch.object(cli, "_start_sync"),
            mock.patch.object(cli, "_spawn_refresh"),
            mock.patch.object(cli, "notify_if_update_available"),
            mock.patch.object(cli, "_create_worktree"),
            mock.patch.object(cli, "_fetch_is_stale", return_value=False),
            mock.patch.object(
                cli, "_claim_checkout", return_value=("work/a", str(self.root / "wt"))
            ),
            mock.patch.object(cli, "read_board", side_effect=refuse),
            mock.patch.object(CacheStore, "refresh", refuse),
            mock.patch.object(CacheStore, "ensure", refuse),
            mock.patch("subprocess.Popen"),
            mock.patch.object(cli, "CLAIM_VERIFY_DELAYS", (0.0, 0.0, 0.0)),
            mock.patch.object(cli, "CLAIM_CONFIRM_DELAY", 0.0),
            # A session never waits for a drainer: it neither drains nor
            # takes the drain lock itself.
            guard_refresh_lock("drain.lock"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = cli.main(["--json", *argv])
        text = stdout.getvalue() or stderr.getvalue()
        self.assertNotIn("BoardRead", text)
        return code, json.loads(text)

    def test_the_drainers_replay_of_each_command_reads_only_its_card(self) -> None:
        # Exactly how execute_intent runs a queued change.
        environment = {"KANBANLAN_SYNC_EXECUTOR": "1", "KANBANLAN_BACKGROUND_REFRESH": "skip"}
        for name, argv, needs in COMMANDS:
            with self.subTest(command=name):
                github = self.github_for(needs)
                snapshot = cached_snapshot(github, age_seconds=0)

                code, payload = self.run_command(
                    argv, github, environment=environment, snapshot=snapshot
                )

                self.assertEqual(0, code, payload)
                self.assertIn("read_request", github.calls)

    def test_a_replayed_capture_reads_nothing_board_wide(self) -> None:
        environment = {"KANBANLAN_SYNC_EXECUTOR": "1", "KANBANLAN_BACKGROUND_REFRESH": "skip"}
        github = OneCardGitHub("Inbox")
        github.find_request = lambda *_a, **_k: None  # type: ignore[method-assign]

        code, payload = self.run_command(
            ["capture", "New", "--kanbanlan-id", IDENTITY],
            github,
            environment=environment,
            snapshot=None,
        )

        self.assertEqual(0, code, payload)
        self.assertIn("create", github.calls)

    def test_each_command_decides_without_the_board_whatever_the_snapshot(self) -> None:
        environment = {"KANBANLAN_WRITE_BEHIND": "1"}
        ages = {"fresh": 0.0, "too old to serve": 180.0 * 11, "missing": None}
        for name, argv, needs in COMMANDS:
            for label, age in ages.items():
                with self.subTest(command=name, snapshot=label):
                    self.root = Path(tempfile.mkdtemp(dir=self.directory.name))
                    github = self.github_for(needs)
                    snapshot = None if age is None else cached_snapshot(github, age_seconds=age)

                    code, payload = self.run_command(
                        argv, github, environment=environment, snapshot=snapshot
                    )

                    self.assertEqual(0, code, payload)
                    self.assertEqual("write-behind", payload["result"]["sync"]["mode"])
                    # Only the card itself may be read, and nothing written.
                    self.assertTrue(
                        set(github.calls) <= {"read_request", "find_request"}, github.calls
                    )

    def test_a_queued_capture_reads_and_writes_nothing(self) -> None:
        environment = {"KANBANLAN_WRITE_BEHIND": "1"}
        for label, snapshot in (
            ("fresh", cached_snapshot(OneCardGitHub("Inbox"), age_seconds=0)),
            ("missing", None),
        ):
            with self.subTest(snapshot=label):
                self.root = Path(tempfile.mkdtemp(dir=self.directory.name))
                github = OneCardGitHub("Inbox")

                code, payload = self.run_command(
                    ["capture", "New thing"], github, environment=environment, snapshot=snapshot
                )

                self.assertEqual(0, code, payload)
                self.assertEqual("write-behind", payload["result"]["sync"]["mode"])
                self.assertEqual([], github.calls)

    def test_a_running_drainer_never_holds_up_a_session(self) -> None:
        store = CacheStore(config(), self.root / "cache")
        box = Outbox(store.directory)
        box._prepare()
        github = OneCardGitHub("Inbox")
        # Another session's drainer is mid-batch and holds the drain lock.
        with locks.FileLock(box.drain_lock_path):
            code, payload = self.run_command(
                ["triage", IDENTITY],
                github,
                environment={"KANBANLAN_WRITE_BEHIND": "1"},
                snapshot=cached_snapshot(github, age_seconds=0),
            )

        self.assertEqual(0, code, payload)
        self.assertEqual("Ready", payload["result"]["status"])

    def test_an_issue_off_the_configured_project_is_refused_not_added(self) -> None:
        for name, environment, argv in (
            (
                "replay",
                {"KANBANLAN_SYNC_EXECUTOR": "1", "KANBANLAN_BACKGROUND_REFRESH": "skip"},
                ["close", "7", "--reason", "x", "--force"],
            ),
            ("replay review", {"KANBANLAN_SYNC_EXECUTOR": "1"}, ["review", "7"]),
            ("typed", {"KANBANLAN_WRITE_BEHIND": "1"}, ["close", "7", "--reason", "x"]),
        ):
            with self.subTest(path=name):
                self.root = Path(tempfile.mkdtemp(dir=self.directory.name))
                github = OneCardGitHub("Inbox", on_board=False, pull_request=True)

                code, payload = self.run_command(
                    argv, github, environment=environment, snapshot=None
                )

                self.assertEqual(1, code)
                self.assertIn("not on the configured kanban home", payload["error"]["message"])
                self.assertEqual(["read_request"], github.calls)

    def test_a_claim_that_never_becomes_visible_fails_closed(self) -> None:
        environment = {"KANBANLAN_SYNC_EXECUTOR": "1", "KANBANLAN_BACKGROUND_REFRESH": "skip"}
        github = OneCardGitHub("Ready")
        github.hidden_prefix = "CLAIM:"

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            environment=environment,
            snapshot=cached_snapshot(OneCardGitHub("Ready"), age_seconds=0),
        )

        self.assertEqual(1, code)
        self.assertIn("claimed first", payload["error"]["message"])
        self.assertNotIn("status:In progress", github.calls)
        self.assertTrue(github.comments[-1]["body"].startswith("RELEASED:"))

    def test_a_claim_is_confirmed_by_a_second_read_before_it_moves_the_card(self) -> None:
        environment = {"KANBANLAN_SYNC_EXECUTOR": "1", "KANBANLAN_BACKGROUND_REFRESH": "skip"}
        github = OneCardGitHub("Ready")
        read = github.read_request
        reads: list[int] = []

        def lagging(reference: Any, **kwargs: Any) -> dict[str, Any]:
            reads.append(1)
            if len(reads) == 3:
                # A replica catches up: an earlier claim from elsewhere appears.
                github.comments.insert(
                    0,
                    {
                        "body": "CLAIM: 2026-09-28T00:00:00Z\nSession: elsewhere",
                        "createdAt": "2026-09-28T00:00:00Z",
                        "author": {"login": "other"},
                    },
                )
            return read(reference, **kwargs)

        github.read_request = lagging  # type: ignore[method-assign]

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            environment=environment,
            snapshot=cached_snapshot(OneCardGitHub("Ready"), age_seconds=0),
        )

        self.assertEqual(1, code)
        self.assertIn("claimed first by elsewhere", payload["error"]["message"])
        self.assertEqual(3, len(reads))
        self.assertNotIn("status:In progress", github.calls)

    def test_a_claim_proceeds_during_a_refresh_cooldown(self) -> None:
        # The board refresh is deferred for quota; a claim needs only its card.
        environment = {"KANBANLAN_SYNC_EXECUTOR": "1", "KANBANLAN_BACKGROUND_REFRESH": "skip"}
        github = OneCardGitHub("Ready")
        reset = isoformat(datetime.now(UTC) + timedelta(minutes=20))
        cached = cached_snapshot(OneCardGitHub("Ready"), age_seconds=0)
        cached["rate_limit"] = {"remaining": 10, "resetAt": reset}
        store = CacheStore(config(), self.root / "cache")
        store._write_json(store.snapshot_path, cached)
        store.record_rate_limit(RateLimitError("GitHub refresh deferred", reset_at=reset))
        self.assertIsNotNone(store.rate_limit_deferral(cached))

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            environment=environment,
            snapshot=cached,
        )

        self.assertEqual(0, code, payload)
        self.assertEqual("s1", payload["result"]["session"])
        self.assertIn("status:In progress", github.calls)

    def test_a_claim_lost_to_another_machine_fails_its_change_without_waiting(self) -> None:
        environment = {"KANBANLAN_SYNC_EXECUTOR": "1", "KANBANLAN_BACKGROUND_REFRESH": "skip"}
        github = OneCardGitHub("Ready")
        comment = github.comment_request

        def race(reference: Any, body: str, **kwargs: Any) -> None:
            if body.startswith("CLAIM:") and not github.comments:
                # Another machine's claim lands first.
                comment(reference, "CLAIM: 2026-09-29T00:00:00Z\nSession: elsewhere")
            comment(reference, body, **kwargs)

        github.comment_request = race  # type: ignore[method-assign]

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            environment=environment,
            snapshot=cached_snapshot(OneCardGitHub("Ready"), age_seconds=0),
        )

        self.assertEqual(1, code)
        self.assertIn("claimed first by elsewhere", payload["error"]["message"])
        self.assertTrue(github.comments[-1]["body"].startswith("RELEASED:"))

    def test_the_drainer_never_refreshes_the_board_itself(self) -> None:
        store = CacheStore(config(), self.root / "cache")
        box = Outbox(store.directory)
        box.write(
            Intent.create(
                kind="triage",
                reference=IDENTITY,
                label=IDENTITY,
                argv=["triage", IDENTITY],
                effect={"status": "Ready"},
                kanbanlan_id=IDENTITY,
            )
        )
        executed: list[str] = []

        def execute(value: Intent) -> outbox.Execution:
            executed.append(value.kind)
            return outbox.Execution(True)

        with (
            mock.patch.object(outbox, "execute_intent", return_value=execute),
            mock.patch.object(CacheStore, "refresh", side_effect=BoardRead("refreshed")),
            mock.patch("subprocess.Popen") as spawned,
            guard_refresh_lock(),
        ):
            drain_outbox(self.root, store, OneCardGitHub("Inbox"))

        self.assertEqual(["triage"], executed)
        # The refresh runs detached, and the change overlays until it lands.
        self.assertIn("refresh", spawned.call_args.args[0])
        self.assertNotIn(QUEUED, [value.state for value in box.intents()])


if __name__ == "__main__":
    unittest.main()
