"""The start, done, and drop verbs, and the admin command group."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from board_fixtures import ALPHA, BETA, REPOSITORY, config, item, snapshot
from cli_patch import patch_cli

from kanbanlan.cli.app import main
from kanbanlan.cli.output import _emit_result
from kanbanlan.cli.parser import ADMIN_COMMANDS, VISIBLE_COMMANDS, build_parser
from kanbanlan.cli.verbs import DEFAULT_DROP_REASON, _prune_orphaned_worktrees, _pull_request_body
from kanbanlan.runner import CommandResult
from kanbanlan.snapshot import CacheStore
from kanbanlan.worktrees import (
    PRUNE,
    REMOVE,
    CleanupAction,
    WorktreeEntry,
    needs_inspection,
    settled_requests,
)

NEW = "KBL-CCCCCCCCCCCCCCCCCCCCCCCCCC"
WORKTREE = "/repo/.worktrees/kbl-aaaaaaaaaaaaaaaaaaaaaaaaaa-request-7"
BRANCH = "work/kbl-aaaaaaaaaaaaaaaaaaaaaaaaaa-request-7"
PULL_REQUEST = {
    "provider_ref": f"github:{REPOSITORY}#11",
    "repository": REPOSITORY,
    "url": "https://github.test/acme/widget/pull/11",
    "linked_by": ["closing_reference"],
}


def result(stdout: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(args=(), returncode=returncode, stdout=stdout, stderr="")


class FakeRunner:
    """Records every command and answers from a table of argv prefixes."""

    def __init__(self, answers: dict[tuple[str, ...], str] | None = None):
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def run(self, args: list[str], **_kwargs: Any) -> CommandResult:
        self.calls.append(list(args))
        for prefix, stdout in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix:
                return result(stdout)
        return result()


class VerbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = CacheStore(config(), self.root / "cache")
        self.provider = mock.Mock()
        self.provider.capabilities.project_scope = True
        patches = [
            patch_cli("_context", return_value=(self.root, config(), self.provider, self.store)),
            patch_cli("_actor_session", return_value=None),
            patch_cli("notify_if_update_available"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write_snapshot(self, items: list[dict[str, Any]]) -> None:
        self.store._prepare_directory()
        self.store._write_json(self.store.snapshot_path, snapshot(items))

    def write_project(self, items: list[dict[str, Any]]) -> None:
        # The overlap check reads the whole Project live.
        self.provider.snapshot.return_value = snapshot(items)

    def run_cli(self, *argv: str, json_output: bool = True) -> tuple[int, Any, str]:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([*(["--json"] if json_output else []), *argv])
        text = stdout.getvalue() or stderr.getvalue()
        try:
            return code, json.loads(text), text
        except json.JSONDecodeError:
            return code, None, text

    def record_steps(self, *names: str) -> None:
        """Stand in for lifecycle commands, recording each step a verb runs."""

        self.steps: list[tuple[str, Any]] = []
        for name in names:
            patcher = patch_cli(f"_cmd_{name}", side_effect=self._recorder(name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _recorder(self, name: str) -> Any:
        def run(args: Any) -> int:
            self.steps.append((name, args))
            gate = getattr(args, "claim_gate", None)
            if gate is not None:
                # The real claim runs its gate before it moves the card.
                gate()
            reference = getattr(args, "issue", None) or NEW
            results = {
                "capture": {"kanbanlan_id": NEW, "provider_ref": f"github:{REPOSITORY}#9"},
                "triage": {"kanbanlan_id": reference, "status": "Ready"},
                "claim": {
                    "kanbanlan_id": reference,
                    "provider_ref": f"github:{REPOSITORY}#9",
                    "session": getattr(args, "session", None),
                    "branch": BRANCH,
                    "worktree": WORKTREE,
                },
                "release": {"kanbanlan_id": reference, "status": "Ready"},
                "close": {
                    "kanbanlan_id": reference,
                    "status": "Done",
                    "close_reason": "not_planned"
                    if getattr(args, "not_planned", False)
                    else "completed",
                },
            }
            _emit_result(args, results[name])
            return 0

        return run

    @property
    def kinds(self) -> list[str]:
        return [name for name, _ in self.steps]


class ParserTests(unittest.TestCase):
    def test_top_level_help_lists_only_the_everyday_commands(self) -> None:
        text = build_parser().format_help()

        listed = text.split("positional arguments:")[1].split("options:")[0]
        names = [
            line.split()[0]
            for line in listed.splitlines()
            if line.startswith("    ") and not line.startswith("     ")
        ]
        self.assertEqual(list(VISIBLE_COMMANDS), names)
        self.assertIn("{init,start,done,drop,status,next,doctor,admin}", text)
        self.assertIn("kanbanlan start", text)

    def test_admin_help_lists_every_other_command(self) -> None:
        parser = build_parser()
        admin = parser._subparsers._group_actions[0].choices["admin"]  # noqa: SLF001
        listed = admin.format_help()

        names = [
            line.split()[0]
            for line in listed.split("positional arguments:")[1].splitlines()
            if line.startswith("    ") and not line.startswith("     ")
        ]
        self.assertEqual(sorted(ADMIN_COMMANDS), sorted(names))

    def test_every_admin_command_still_parses_at_the_top_level(self) -> None:
        parser = build_parser()
        for name in ADMIN_COMMANDS:
            with self.subTest(name=name):
                hidden = parser._subparsers._group_actions[0].choices[name]  # noqa: SLF001
                self.assertIsNotNone(hidden)

    def test_admin_and_hidden_forms_dispatch_to_one_handler(self) -> None:
        for argv in (["admin", "path"], ["path"]):
            with (
                self.subTest(argv=argv),
                patch_cli("_cmd_path", return_value=0) as handler,
                patch_cli("notify_if_update_available"),
            ):
                self.assertEqual(0, main(argv))
                (args,) = handler.call_args.args
                self.assertEqual("path", args.command)


class StartTests(VerbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_project([])
        self.cleanup_plan = patch_cli("_plan_worktree_cleanup", return_value=[])
        self.cleanup_plan.start()
        self.addCleanup(self.cleanup_plan.stop)
        for name, value in (("_claim_checkout", (BRANCH, WORKTREE)),):
            patcher = patch_cli(name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.record_steps("capture", "triage", "claim")

    def test_a_title_is_captured_triaged_and_claimed_in_one_call(self) -> None:
        self.write_snapshot([])

        code, payload, _ = self.run_cli(
            "start", "Add a thing", "--touchpoints", "cli.py", "--session", "s1"
        )

        self.assertEqual(0, code)
        value = payload["result"]
        self.assertTrue(value["captured"])
        self.assertTrue(value["triaged"])
        self.assertEqual(WORKTREE, value["worktree"])
        self.assertEqual("s1", value["session"])
        self.assertEqual(["capture", "triage", "claim"], self.kinds)
        claim = self.steps[-1][1]
        self.assertEqual("cli.py", claim.touchpoints)
        self.assertEqual(value["request"]["kanbanlan_id"], claim.issue)

    def test_a_ready_reference_is_claimed_without_capture_or_triage(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Ready")])

        code, payload, _ = self.run_cli("start", "#7", "--session", "s1")

        self.assertEqual(0, code)
        self.assertFalse(payload["result"]["captured"])
        self.assertFalse(payload["result"]["triaged"])
        self.assertEqual(["claim"], self.kinds)
        self.assertEqual(ALPHA, self.steps[0][1].issue)

    def test_an_inbox_reference_is_triaged_first(self) -> None:
        self.write_snapshot([item(7, ALPHA, "Inbox")])

        code, payload, _ = self.run_cli("start", ALPHA, "--session", "s1")

        self.assertEqual(0, code)
        self.assertTrue(payload["result"]["triaged"])
        self.assertEqual(["triage", "claim"], self.kinds)

    def test_a_card_claimed_by_someone_else_is_refused(self) -> None:
        self.write_snapshot([item(7, ALPHA, "In progress", claim={"session": "other"})])

        code, payload, _ = self.run_cli("start", "#7", "--session", "s1")

        self.assertEqual(1, code)
        self.assertIn("already claimed by other", payload["error"]["message"])
        self.assertEqual([], self.steps)

    def test_a_session_that_already_owns_a_card_is_refused(self) -> None:
        self.write_snapshot(
            [
                item(7, ALPHA, "In progress", claim={"session": "s1"}),
                item(8, BETA, "Ready"),
            ]
        )

        code, payload, _ = self.run_cli("start", "#8", "--session", "s1")

        self.assertEqual(1, code)
        self.assertIn("already owns", payload["error"]["message"])
        self.assertEqual([], self.steps)

    def test_a_new_title_is_refused_before_capture_when_the_session_owns_a_card(self) -> None:
        self.write_snapshot([])
        self.write_project([item(7, ALPHA, "In progress", claim={"session": "s1"})])

        code, payload, _ = self.run_cli("start", "Another thing", "--session", "s1")

        self.assertEqual(1, code)
        self.assertIn("already owns", payload["error"]["message"])
        self.assertEqual([], self.steps)

    def test_starting_the_card_this_session_owns_continues_it(self) -> None:
        claim = {"session": "s1", "branch": BRANCH, "worktree": WORKTREE}
        self.write_snapshot([item(7, ALPHA, "In progress", claim=claim)])

        code, payload, _ = self.run_cli("start", "#7", "--session", "s1")

        self.assertEqual(0, code)
        self.assertTrue(payload["result"]["resumed"])
        self.assertEqual(WORKTREE, payload["result"]["worktree"])
        self.assertEqual([], self.steps)

    def test_other_active_claims_are_reported_with_their_touchpoints(self) -> None:
        self.write_snapshot([item(8, BETA, "Ready")])
        self.write_project(
            [
                item(8, BETA, "Ready"),
                item(7, ALPHA, "In progress", claim={"session": "other", "touchpoints": "cli.py"}),
            ]
        )

        code, payload, _ = self.run_cli("start", "#8", "--session", "s1")

        self.assertEqual(0, code)
        (other,) = payload["result"]["overlap"]["active_claims"]
        self.assertEqual(("other", "cli.py"), (other["session"], other["touchpoints"]))
        self.assertEqual(ALPHA, other["kanbanlan_id"])

    def test_human_output_ends_with_the_worktree_path(self) -> None:
        self.write_snapshot([item(8, BETA, "Ready")])
        self.write_project(
            [item(7, ALPHA, "In progress", claim={"session": "other", "touchpoints": "docs"})]
        )

        code, _, text = self.run_cli("start", "#8", "--session", "s1", json_output=False)

        self.assertEqual(0, code)
        self.assertIn("docs", text)
        self.assertEqual(WORKTREE, text.strip().splitlines()[-1])

    def test_pruned_worktrees_are_reported(self) -> None:
        self.write_snapshot([item(8, BETA, "Ready")])
        removed = [{"path": "/repo/.worktrees/old", "action": REMOVE, "reason": "claim released"}]
        with patch_cli("_prune_candidates", return_value=removed):
            code, payload, _ = self.run_cli("start", "#8", "--session", "s1")

        self.assertEqual(0, code)
        self.assertEqual(removed, payload["result"]["pruned"])

    def test_a_ready_card_goes_to_its_claim_with_the_project_check_as_its_gate(self) -> None:
        # The local view already checked the card, so the claim skips its
        # own read before posting, and checks the Project before moving it.
        target = item(8, BETA, "Ready")
        self.write_snapshot([target])

        code, _, _ = self.run_cli("start", "#8", "--session", "s1")

        self.assertEqual(0, code)
        (claim,) = [args for name, args in self.steps if name == "claim"]
        self.assertEqual(target["provider_ref"], claim.known_card["provider_ref"])
        self.assertTrue(callable(claim.claim_gate))

    def test_a_peer_card_this_session_owns_refuses_the_claim_at_its_gate(self) -> None:
        self.write_snapshot([item(8, BETA, "Ready")])
        self.write_project([item(7, ALPHA, "In progress", claim={"session": "s1"})])

        code, payload, _ = self.run_cli("start", "#8", "--session", "s1")

        self.assertEqual(1, code)
        self.assertIn("already owns", payload["error"]["message"])
        self.assertEqual(["claim"], self.kinds)

    def test_an_inbox_card_is_checked_against_the_project_before_its_triage(self) -> None:
        self.write_snapshot([item(8, BETA, "Inbox")])
        self.write_project([item(7, ALPHA, "In progress", claim={"session": "s1"})])

        code, payload, _ = self.run_cli("start", "#8", "--session", "s1")

        self.assertEqual(1, code)
        self.assertIn("already owns", payload["error"]["message"])
        self.assertEqual([], self.steps)


class PruneTests(VerbTestCase):
    def action(self, kind: str) -> CleanupAction:
        return CleanupAction(path="/repo/.worktrees/old", action=kind, reason="claim was released")

    def test_nothing_orphaned_costs_no_github_read(self) -> None:
        with patch_cli("_plan_worktree_cleanup", return_value=[self.action("keep")]) as plan:
            pruned = _prune_orphaned_worktrees(
                self.root, config(), self.provider, self.store, snapshot([])
            )

        self.assertEqual([], pruned)
        self.assertFalse(plan.call_args.kwargs["fetch"])
        self.assertFalse(plan.call_args.kwargs["force"])
        self.provider.snapshot.assert_not_called()

    def test_a_candidate_is_confirmed_exactly_as_cleanup_decides(self) -> None:
        refreshed = snapshot([])
        self.store.refresh = mock.Mock(return_value=refreshed)  # type: ignore[method-assign]
        confirmed = [self.action(PRUNE)]
        with (
            patch_cli(
                "_plan_worktree_cleanup",
                side_effect=[[self.action(REMOVE)], confirmed],
            ) as plan,
            patch_cli("_apply_cleanup", return_value=["done"]) as apply,
        ):
            pruned = _prune_orphaned_worktrees(
                self.root, config(), self.provider, self.store, snapshot([])
            )

        self.assertEqual(["done"], pruned)
        second = plan.call_args_list[1]
        self.assertFalse(second.kwargs["force"])
        self.assertNotIn("fetch", second.kwargs)
        self.assertEqual(confirmed, apply.call_args.args[1])

    def test_a_candidate_the_live_board_keeps_is_left_alone(self) -> None:
        self.store.refresh = mock.Mock(return_value=snapshot([]))  # type: ignore[method-assign]
        with (
            patch_cli(
                "_plan_worktree_cleanup",
                side_effect=[[self.action(REMOVE)], [self.action("keep")]],
            ),
            patch_cli("_apply_cleanup") as apply,
        ):
            pruned = _prune_orphaned_worktrees(
                self.root, config(), self.provider, self.store, snapshot([])
            )

        self.assertEqual([], pruned)
        apply.assert_not_called()


class InspectionTests(unittest.TestCase):
    def test_only_worktrees_of_unclaimed_requests_are_inspected(self) -> None:
        settled = settled_requests(
            snapshot(
                [
                    item(7, ALPHA, "Ready"),
                    item(8, BETA, "In progress", claim={"session": "s1"}),
                ]
            )
        )
        released = WorktreeEntry(path=WORKTREE, branch=BRANCH)
        claimed = WorktreeEntry(path="/repo/.worktrees/kbl-bbbbbbbbbbbbbbbbbbbbbbbbbb-x")
        unlinked = WorktreeEntry(path="/repo/.worktrees/scratch", branch="scratch")
        locked = WorktreeEntry(path=WORKTREE, branch=BRANCH, locked=True)

        self.assertTrue(needs_inspection(released, settled))
        self.assertFalse(needs_inspection(claimed, settled))
        self.assertFalse(needs_inspection(unlinked, settled))
        self.assertFalse(needs_inspection(locked, settled))


class DoneTests(VerbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.git = FakeRunner({("git", "branch", "--show-current"): f"{BRANCH}\n"})
        self.gh = FakeRunner()
        for name, value in (("Runner", lambda *_args, **_kwargs: self.git),):
            patcher = patch_cli(name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch_cli("_gh_runner", return_value=self.gh)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch_cli("DONE_LINK_DELAYS", (0.0, 0.0))
        patcher.start()
        self.addCleanup(patcher.stop)
        claim = {"session": "s1", "branch": BRANCH, "worktree": WORKTREE}
        self.claimed = item(7, ALPHA, "In progress", claim=claim)
        self.write_snapshot([self.claimed])
        self.provider.read_request.return_value = {
            **snapshot([]),
            "items": [{**self.claimed, "linked_open_pull_requests": [PULL_REQUEST]}],
        }

    def gh_calls(self, verb: str) -> list[list[str]]:
        return [call for call in self.gh.calls if call[:3] == ["gh", "pr", verb]]

    def test_done_opens_a_pull_request_and_moves_the_request_to_review(self) -> None:
        self.gh.answers = {
            ("gh", "pr", "list"): "[]",
            ("gh", "pr", "create"): f"{PULL_REQUEST['url']}\n",
        }

        code, payload, _ = self.run_cli("done")

        self.assertEqual(0, code)
        value = payload["result"]
        self.assertTrue(value["created_pull_request"])
        self.assertEqual(PULL_REQUEST["url"], value["pull_request"]["url"])
        self.assertEqual("In review", value["review"]["status"])
        (create,) = self.gh_calls("create")
        body = create[create.index("--body") + 1]
        self.assertIn(f"Kanbanlan: {ALPHA}", body)
        self.assertIn("Closes #7", body)
        self.assertEqual(BRANCH, create[create.index("--head") + 1])
        self.assertIn(["git", "push", "--set-upstream", "origin", BRANCH], self.git.calls)
        self.provider.set_request_status.assert_called_once_with(7, "status:review")

    def test_done_reuses_an_open_pull_request(self) -> None:
        self.gh.answers = {("gh", "pr", "list"): json.dumps([{"url": PULL_REQUEST["url"]}])}

        code, payload, _ = self.run_cli("done", ALPHA)

        self.assertEqual(0, code)
        self.assertFalse(payload["result"]["created_pull_request"])
        self.assertEqual([], self.gh_calls("create"))
        self.assertNotIn(["git", "push", "--set-upstream", "origin", BRANCH], self.git.calls)

    def test_done_waits_briefly_for_a_new_pull_request_to_link(self) -> None:
        self.gh.answers = {
            ("gh", "pr", "list"): "[]",
            ("gh", "pr", "create"): f"{PULL_REQUEST['url']}\n",
        }
        unlinked = {**snapshot([]), "items": [self.claimed]}
        linked = self.provider.read_request.return_value
        self.provider.read_request.side_effect = [unlinked, linked]

        code, payload, _ = self.run_cli("done")

        self.assertEqual(0, code)
        self.assertEqual("In review", payload["result"]["review"]["status"])

    def test_done_outside_a_request_worktree_asks_for_a_reference(self) -> None:
        self.git.answers = {("git", "branch", "--show-current"): "main\n"}
        self.write_snapshot([])

        code, payload, _ = self.run_cli("done")

        self.assertEqual(1, code)
        self.assertIn("pass its Kanbanlan ID", payload["error"]["message"])


class DropTests(VerbTestCase):
    def setUp(self) -> None:
        super().setUp()
        git = FakeRunner({("git", "branch", "--show-current"): f"{BRANCH}\n"})
        patcher = patch_cli("Runner", return_value=git)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_snapshot([item(7, ALPHA, "In progress", claim={"session": "s1"})])
        self.record_steps("release", "close")

    def test_drop_releases_this_worktrees_claim(self) -> None:
        code, payload, _ = self.run_cli("drop")

        self.assertEqual(0, code)
        self.assertEqual("Ready", payload["result"]["status"])
        ((kind, step),) = self.steps
        self.assertEqual("release", kind)
        self.assertEqual(DEFAULT_DROP_REASON, step.reason)

    def test_drop_close_closes_the_request(self) -> None:
        code, payload, _ = self.run_cli(
            "drop", ALPHA, "--close", "--reason", "duplicate", "--not-planned"
        )

        self.assertEqual(0, code)
        self.assertEqual("not_planned", payload["result"]["close_reason"])
        ((kind, step),) = self.steps
        self.assertEqual("close", kind)
        self.assertTrue(step.not_planned)

    def test_drop_close_needs_a_reason(self) -> None:
        code, payload, _ = self.run_cli("drop", "--close")

        self.assertEqual(1, code)
        self.assertIn("--close requires --reason", payload["error"]["message"])
        self.assertEqual([], self.steps)

    def test_close_only_flags_need_close(self) -> None:
        code, payload, _ = self.run_cli("drop", "--not-planned")

        self.assertEqual(1, code)
        self.assertIn("only apply with --close", payload["error"]["message"])


class PullRequestBodyTests(unittest.TestCase):
    def test_the_template_is_filled_with_the_request_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            (root / ".github" / "pull_request_template.md").write_text(
                "## Summary\n\nKanbanlan: KBL-\n\nCloses #\n\n## Changes\n", encoding="utf-8"
            )
            body = _pull_request_body(root, config(), item(7, ALPHA, "In progress"))

        self.assertIn(f"Kanbanlan: {ALPHA}\n", body)
        self.assertIn("Closes #7\n", body)
        self.assertIn("## Changes", body)

    def test_a_request_in_another_repository_is_closed_by_its_qualified_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = {**item(7, ALPHA, "In progress"), "repository": "acme/other"}
            body = _pull_request_body(Path(directory), config(), value)

        self.assertIn("Closes acme/other#7", body)
        self.assertIn(f"Kanbanlan: {ALPHA}", body)


if __name__ == "__main__":
    unittest.main()
