"""Lifecycle commands never read the whole board or wait on the refresh lock.

A full read of a large Project takes minutes and serializes every session
on one lock, so a lifecycle command never starts one: it reads and writes
only its own card. Each test below runs a command against a fake GitHub that
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

from cli_patch import patch_cli

from kanbanlan import locks
from kanbanlan.cli.app import main
from kanbanlan.config import Config
from kanbanlan.identity import attach_kanbanlan_id
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
    """Fail on waiting for ``refresh.lock``, or on entering any other lock named.

    Writing a card into the snapshot only tries ``refresh.lock`` without
    waiting (``timeout=0``), which can never queue a command behind a refresh.
    """

    enter = locks.FileLock.__enter__
    named = set(names)

    def guarded(self: locks.FileLock) -> locks.FileLock:
        name = Path(self.path).name
        if name in named or (name == "refresh.lock" and self.timeout > 0):
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


def _parser_with(handoff: dict[str, Any]) -> Any:
    """Build the real parser, adding what 'start' hands a claim step."""

    from kanbanlan.cli.parser import build_parser

    def build() -> Any:
        parser = build_parser()
        parse = parser.parse_args

        def parse_args(argv: Any = None) -> Any:
            args = parse(argv)
            for name, value in handoff.items():
                setattr(args, name, value)
            return args

        parser.parse_args = parse_args  # type: ignore[method-assign]
        return parser

    return build


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
        snapshot: dict[str, Any] | None,
        environment: dict[str, str] | None = None,
        handoff: dict[str, Any] | None = None,
        create_worktree: Any = None,
    ) -> tuple[int, dict[str, Any]]:
        store = CacheStore(config(), self.root / "cache")
        if snapshot is not None:
            store._write_json(store.snapshot_path, snapshot)

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise BoardRead("refreshed the shared snapshot")

        stdout, stderr = StringIO(), StringIO()
        with (
            mock.patch.dict(os.environ, environment or {}),
            patch_cli("_context", return_value=(self.root, config(), github, store)),
            patch_cli("_actor_session", return_value=None),
            patch_cli("notify_if_update_available"),
            patch_cli("_create_worktree", create_worktree or mock.Mock(return_value=None)),
            patch_cli("_fetch_default_branch", return_value=True),
            mock.patch("kanbanlan.cli.app.build_parser", _parser_with(handoff or {})),
            patch_cli("_claim_checkout", return_value=("work/a", str(self.root / "wt"))),
            patch_cli("read_board", side_effect=refuse),
            mock.patch.object(CacheStore, "refresh", refuse),
            mock.patch.object(CacheStore, "ensure", refuse),
            mock.patch("subprocess.Popen"),
            patch_cli("CLAIM_VERIFY_DELAYS", (0.0, 0.0, 0.0)),
            patch_cli("CLAIM_CONFIRM_DELAY", 0.0),
            guard_refresh_lock(),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(["--json", *argv])
        text = stdout.getvalue() or stderr.getvalue()
        self.assertNotIn("BoardRead", text)
        return code, json.loads(text)

    def test_each_command_reads_and_writes_only_its_card_whatever_the_snapshot(self) -> None:
        ages = {"fresh": 0.0, "old": 180.0 * 11, "missing": None}
        for name, argv, needs in COMMANDS:
            for label, age in ages.items():
                with self.subTest(command=name, snapshot=label):
                    self.root = Path(tempfile.mkdtemp(dir=self.directory.name))
                    github = self.github_for(needs)
                    snapshot = None if age is None else cached_snapshot(github, age_seconds=age)

                    code, payload = self.run_command(argv, github, snapshot=snapshot)

                    self.assertEqual(0, code, payload)
                    self.assertIn("read_request", github.calls)
                    self.assertNotIn("sync", payload["result"])

    def test_capture_reads_nothing_board_wide(self) -> None:
        for argv in (["capture", "New"], ["capture", "New", "--kanbanlan-id", IDENTITY]):
            with self.subTest(argv=argv):
                self.root = Path(tempfile.mkdtemp(dir=self.directory.name))
                github = OneCardGitHub("Inbox")
                github.find_request = lambda *_a, **_k: None  # type: ignore[method-assign]

                code, payload = self.run_command(argv, github, snapshot=None)

                self.assertEqual(0, code, payload)
                self.assertIn("create", github.calls)
                self.assertIsNotNone(payload["result"]["url"])

    def test_a_mutation_writes_its_card_into_the_snapshot_without_reading_the_board(
        self,
    ) -> None:
        github = OneCardGitHub("Inbox")
        store = CacheStore(config(), self.root / "cache")

        code, payload = self.run_command(
            ["triage", IDENTITY], github, snapshot=cached_snapshot(github, age_seconds=0)
        )

        self.assertEqual(0, code, payload)
        self.assertTrue(store.is_fresh())
        (card,) = store.snapshot()["items"]
        self.assertEqual("Ready", card["status"])
        self.assertIn("status:ready", [label["name"] for label in card["labels"]])
        self.assertEqual(IDENTITY, store.snapshot()["next_ready"]["kanbanlan_id"])

    def test_a_claim_writes_itself_into_the_snapshot(self) -> None:
        github = OneCardGitHub("Ready")
        store = CacheStore(config(), self.root / "cache")

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            snapshot=cached_snapshot(OneCardGitHub("Ready"), age_seconds=0),
        )

        self.assertEqual(0, code, payload)
        self.assertTrue(store.is_fresh())
        (card,) = store.snapshot()["items"]
        self.assertEqual(("In progress", "s1"), (card["status"], card["active_claim"]["session"]))
        self.assertIsNone(store.snapshot()["next_ready"])

    def test_a_mutation_during_a_refresh_marks_the_snapshot_stale(self) -> None:
        # A refresh holding the lock may have read GitHub before this write.
        github = OneCardGitHub("Inbox")
        store = CacheStore(config(), self.root / "cache")
        store._prepare_directory()

        with (
            locks.FileLock(store.lock_path),
            # The owner check runs ps, which run_command stubs out.
            mock.patch.object(locks, "owner_predates_lock", return_value=True),
        ):
            code, payload = self.run_command(
                ["triage", IDENTITY], github, snapshot=cached_snapshot(github, age_seconds=0)
            )

        self.assertEqual(0, code, payload)
        self.assertFalse(store.is_fresh())
        self.assertEqual("Inbox", store.snapshot()["items"][0]["status"])

    def test_a_claim_written_and_read_in_one_call_is_only_read_again_to_confirm(self) -> None:
        class OneCallGitHub(OneCardGitHub):
            def comment_and_read_request(self, content_id: str, body: str, **_kwargs: Any) -> Any:
                self.calls.append(f"comment_and_read:{content_id}")
                self.comment_request(7, body)
                self.calls.pop()
                return build_snapshot(config(), self._project(), [], {})

        github = OneCallGitHub("Ready")

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            snapshot=cached_snapshot(OneCardGitHub("Ready"), age_seconds=0),
        )

        self.assertEqual(0, code, payload)
        # The read before the claim, the claim itself, and the confirming read.
        posted = github.calls.index("comment_and_read:issue-7")
        self.assertEqual(["read_request"], github.calls[:posted])
        self.assertEqual(1, github.calls[posted:].count("read_request"))
        self.assertIn("status:In progress", github.calls)

    def test_a_handed_over_card_is_claimed_without_a_read_before_its_comment(self) -> None:
        github = OneCardGitHub("Ready")
        known = cached_snapshot(OneCardGitHub("Ready"), age_seconds=0)["items"][0]

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            snapshot=None,
            handoff={"known_card": known},
        )

        self.assertEqual(0, code, payload)
        self.assertEqual("comment", github.calls[0])
        self.assertIn("status:In progress", github.calls)

    def test_a_handed_over_card_that_moved_is_released_not_claimed(self) -> None:
        github = OneCardGitHub("Blocked")
        known = cached_snapshot(OneCardGitHub("Ready"), age_seconds=0)["items"][0]
        create = mock.Mock(return_value=None)

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1"],
            github,
            snapshot=None,
            handoff={"known_card": known},
            create_worktree=create,
        )

        self.assertEqual(1, code)
        self.assertIn("'Blocked', not Ready", payload["error"]["message"])
        self.assertTrue(github.comments[-1]["body"].startswith("RELEASED:"))
        self.assertFalse([call for call in github.calls if call.startswith("status:")])
        # A claim that does not hold never takes the worktree path.
        create.assert_not_called()

    def test_a_lost_claim_never_adds_the_worktree(self) -> None:
        # Two sessions on one machine compute the same worktree path; only
        # the winner may add it, or the loser's worktree would make the
        # winner's fail.
        github = OneCardGitHub("Ready")
        create = mock.Mock(return_value=None)

        def rival_claim() -> None:
            raise RuntimeError("session s1 already owns github:acme/other#3")

        code, _ = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1"],
            github,
            snapshot=None,
            handoff={"claim_gate": rival_claim},
            create_worktree=create,
        )

        self.assertEqual(1, code)
        create.assert_not_called()

    def test_a_claim_whose_read_fails_after_posting_is_released(self) -> None:
        class FailingReadGitHub(OneCardGitHub):
            def read_request(self, *args: Any, **kwargs: Any) -> Any:
                if self.comments:
                    raise RuntimeError("GitHub unavailable")
                return super().read_request(*args, **kwargs)

        github = FailingReadGitHub("Ready")

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            snapshot=None,
        )

        self.assertEqual(1, code)
        self.assertIn("GitHub unavailable", payload["error"]["message"])
        self.assertIn("claim interrupted", github.comments[-1]["body"])
        self.assertFalse([call for call in github.calls if call.startswith("status:")])

    def test_a_claim_its_gate_refuses_is_released_before_the_card_moves(self) -> None:
        github = OneCardGitHub("Ready")

        def gate() -> None:
            raise RuntimeError("session s1 already owns github:acme/other#3")

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            snapshot=None,
            handoff={"claim_gate": gate},
        )

        self.assertEqual(1, code)
        self.assertIn("already owns", payload["error"]["message"])
        self.assertTrue(github.comments[-1]["body"].startswith("RELEASED:"))
        self.assertFalse([call for call in github.calls if call.startswith("status:")])

    def test_a_failed_worktree_releases_the_claim_without_moving_the_card(self) -> None:
        github = OneCardGitHub("Ready")

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1"],
            github,
            snapshot=None,
            create_worktree=mock.Mock(side_effect=RuntimeError("worktree path is taken")),
        )

        self.assertEqual(1, code)
        self.assertIn("worktree path is taken", payload["error"]["message"])
        self.assertIn("worktree creation failed", github.comments[-1]["body"])
        self.assertFalse([call for call in github.calls if call.startswith("status:")])

    def test_an_issue_off_the_configured_project_is_refused_not_added(self) -> None:
        for argv in (
            ["close", "7", "--reason", "x", "--force"],
            ["review", "7"],
            ["close", "7", "--reason", "x"],
        ):
            with self.subTest(argv=argv):
                self.root = Path(tempfile.mkdtemp(dir=self.directory.name))
                github = OneCardGitHub("Inbox", on_board=False, pull_request=True)

                code, payload = self.run_command(argv, github, snapshot=None)

                self.assertEqual(1, code)
                self.assertIn("not on the configured kanban home", payload["error"]["message"])
                self.assertEqual(["read_request"], github.calls)

    def test_a_claim_that_never_becomes_visible_fails_closed(self) -> None:
        github = OneCardGitHub("Ready")
        github.hidden_prefix = "CLAIM:"

        code, payload = self.run_command(
            ["claim", IDENTITY, "--touchpoints", "src", "--session", "s1", "--no-worktree"],
            github,
            snapshot=cached_snapshot(OneCardGitHub("Ready"), age_seconds=0),
        )

        self.assertEqual(1, code)
        self.assertIn("claimed first", payload["error"]["message"])
        self.assertNotIn("status:In progress", github.calls)
        self.assertTrue(github.comments[-1]["body"].startswith("RELEASED:"))

    def test_a_claim_is_confirmed_by_a_second_read_before_it_moves_the_card(self) -> None:
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
            snapshot=cached_snapshot(OneCardGitHub("Ready"), age_seconds=0),
        )

        self.assertEqual(1, code)
        self.assertIn("claimed first by elsewhere", payload["error"]["message"])
        self.assertEqual(3, len(reads))
        self.assertNotIn("status:In progress", github.calls)

    def test_a_claim_proceeds_during_a_refresh_cooldown(self) -> None:
        # The board refresh is deferred for quota; a claim needs only its card.
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
            snapshot=cached,
        )

        self.assertEqual(0, code, payload)
        self.assertEqual("s1", payload["result"]["session"])
        self.assertIn("status:In progress", github.calls)

    def test_a_claim_lost_to_another_machine_fails_without_waiting(self) -> None:
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
            snapshot=cached_snapshot(OneCardGitHub("Ready"), age_seconds=0),
        )

        self.assertEqual(1, code)
        self.assertIn("claimed first by elsewhere", payload["error"]["message"])
        self.assertTrue(github.comments[-1]["body"].startswith("RELEASED:"))


if __name__ == "__main__":
    unittest.main()
