from __future__ import annotations

import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from kanbanlan.cli import _cmd_cleanup, build_parser
from kanbanlan.config import Config
from kanbanlan.runner import CommandResult
from kanbanlan.worktrees import (
    KEEP,
    PRUNE,
    REMOVE,
    WorktreeEntry,
    WorktreeStatus,
    format_action,
    inspect_worktree,
    parse_worktree_list,
    plan_cleanup,
    worktree_identity,
)

REPOSITORY = "acme/widget"
IDENTITY = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER_IDENTITY = "KBL-BBBBBBBBBBBBBBBBBBBBBBBBBB"
MAIN = "/repo"
SLUG = "kbl-aaaaaaaaaaaaaaaaaaaaaaaaaa-add-a-thing"
WORKTREE = f"/repo/.worktrees/{SLUG}"
BRANCH = f"work/{SLUG}"
UPSTREAM = f"origin/{BRANCH}"


def config() -> Config:
    return Config(
        repository=REPOSITORY,
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def item(
    *,
    kanbanlan_id: str = IDENTITY,
    state: str = "CLOSED",
    status: str = "Done",
    claim: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "ISSUE",
        "number": 7,
        "kanbanlan_id": kanbanlan_id,
        "provider_ref": f"github:{REPOSITORY}#7",
        "display_id": "#7",
        "title": "Add a thing",
        "state": state,
        "status": status,
        "active_claim": claim,
    }


def snapshot(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"items": items if items is not None else [item()]}


def entry(
    path: str = WORKTREE,
    branch: str | None = BRANCH,
    **kwargs: Any,
) -> WorktreeEntry:
    return WorktreeEntry(path=path, branch=branch, **kwargs)


def pushed(**kwargs: Any) -> WorktreeStatus:
    """A branch whose commits are all on the remote, as `claim` leaves it."""

    return WorktreeStatus(upstream=UPSTREAM, **kwargs)


def plan(
    entries: list[WorktreeEntry],
    value: dict[str, Any] | None = None,
    statuses: dict[str, WorktreeStatus] | None = None,
    *,
    current_path: str = MAIN,
    force: bool = False,
):
    return plan_cleanup(
        entries,
        value or snapshot(),
        statuses or {},
        main_path=MAIN,
        current_path=current_path,
        default_branch="main",
        force=force,
    )


class ParseTests(unittest.TestCase):
    def test_parses_paths_branches_and_flags(self) -> None:
        output = (
            "worktree /repo\n"
            "HEAD abc\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {WORKTREE}\n"
            "HEAD def\n"
            f"branch refs/heads/{BRANCH}\n"
            "locked\n"
            "\n"
            "worktree /repo/.worktrees/gone\n"
            "HEAD ghi\n"
            "detached\n"
            "prunable gitdir file points to non-existent location\n"
        )

        entries = parse_worktree_list(output)

        self.assertEqual(["/repo", WORKTREE, "/repo/.worktrees/gone"], [e.path for e in entries])
        self.assertEqual("main", entries[0].branch)
        self.assertEqual(BRANCH, entries[1].branch)
        self.assertTrue(entries[1].locked)
        self.assertTrue(entries[2].detached)
        self.assertTrue(entries[2].prunable)
        self.assertIsNone(entries[2].branch)

    def test_parses_a_final_entry_without_a_trailing_blank_line(self) -> None:
        entries = parse_worktree_list("worktree /repo\nHEAD abc\nbranch refs/heads/main")

        self.assertEqual(1, len(entries))
        self.assertEqual("main", entries[0].branch)


class IdentityTests(unittest.TestCase):
    def test_identity_comes_from_the_branch(self) -> None:
        self.assertEqual(IDENTITY, worktree_identity(entry(path="/tmp/anything")))

    def test_identity_falls_back_to_the_directory_name(self) -> None:
        self.assertEqual(IDENTITY, worktree_identity(entry(branch=None)))

    def test_an_unrelated_worktree_has_no_identity(self) -> None:
        self.assertIsNone(worktree_identity(entry(path="/repo/.worktrees/scratch", branch="wip")))


class PlanTests(unittest.TestCase):
    def test_the_primary_worktree_is_never_planned(self) -> None:
        self.assertEqual([], plan([entry(path=MAIN, branch="main")]))

    def test_a_settled_request_removes_its_worktree_and_branch(self) -> None:
        actions = plan([entry()])

        self.assertEqual([REMOVE], [value.action for value in actions])
        self.assertTrue(actions[0].delete_branch)
        self.assertFalse(actions[0].forced)
        self.assertEqual("request is closed", actions[0].reason)
        self.assertIn(BRANCH, format_action(actions[0]))

    def test_a_released_claim_on_an_open_request_still_frees_its_worktree(self) -> None:
        actions = plan([entry()], snapshot([item(state="OPEN", status="Ready")]))

        self.assertEqual([REMOVE], [value.action for value in actions])
        self.assertEqual("claim was released", actions[0].reason)

    def test_an_active_claim_keeps_its_worktree(self) -> None:
        value = snapshot([item(state="OPEN", status="In progress", claim={"session": "claude-1"})])

        actions = plan([entry()], value)

        self.assertEqual([KEEP], [value.action for value in actions])
        self.assertIn("claude-1", actions[0].reason)

    def test_an_unlinked_worktree_is_never_removed(self) -> None:
        actions = plan([entry(path="/repo/.worktrees/scratch", branch="wip")])

        self.assertEqual([KEEP], [value.action for value in actions])
        self.assertIn("not linked", actions[0].reason)

    def test_a_worktree_for_another_board_request_is_kept(self) -> None:
        actions = plan([entry()], snapshot([item(kanbanlan_id=OTHER_IDENTITY)]))

        self.assertEqual([KEEP], [value.action for value in actions])

    def test_the_current_worktree_is_kept(self) -> None:
        actions = plan([entry()], current_path=WORKTREE)

        self.assertEqual([KEEP], [value.action for value in actions])
        self.assertEqual("current worktree", actions[0].reason)

    def test_a_locked_worktree_is_kept(self) -> None:
        actions = plan([entry(locked=True)])

        self.assertEqual([KEEP], [value.action for value in actions])
        self.assertIn("locked", actions[0].reason)

    def test_a_missing_directory_is_pruned(self) -> None:
        actions = plan([entry(prunable=True)])

        self.assertEqual([PRUNE], [value.action for value in actions])

    def test_uncommitted_changes_keep_the_worktree(self) -> None:
        actions = plan([entry()], statuses={WORKTREE: pushed(dirty=True)})

        self.assertEqual([KEEP], [value.action for value in actions])
        self.assertIn("--force", actions[0].reason)

    def test_force_discards_uncommitted_changes(self) -> None:
        actions = plan([entry()], statuses={WORKTREE: pushed(dirty=True)}, force=True)

        self.assertEqual([REMOVE], [value.action for value in actions])
        self.assertTrue(actions[0].forced)
        self.assertTrue(actions[0].delete_branch)

    def test_a_squash_merged_branch_is_delivered_once_it_is_pushed(self) -> None:
        # A squash rewrites the commit, so a delivered branch always reports
        # commits the default branch does not contain.
        actions = plan([entry()], statuses={WORKTREE: pushed(unmerged=1)})

        self.assertEqual([REMOVE], [value.action for value in actions])
        self.assertFalse(actions[0].forced)
        self.assertTrue(actions[0].delete_branch)
        self.assertTrue(actions[0].force_delete_branch)

    def test_a_branch_merged_by_fast_forward_deletes_without_force(self) -> None:
        actions = plan([entry()], statuses={WORKTREE: pushed(unmerged=0)})

        self.assertEqual([REMOVE], [value.action for value in actions])
        self.assertTrue(actions[0].delete_branch)
        self.assertFalse(actions[0].force_delete_branch)

    def test_unpushed_commits_keep_the_worktree(self) -> None:
        actions = plan([entry()], statuses={WORKTREE: pushed(unmerged=3, unpushed=3)})

        self.assertEqual([KEEP], [value.action for value in actions])
        self.assertIn(f"3 commit(s) not pushed to {UPSTREAM}", actions[0].reason)

    def test_a_branch_with_no_upstream_falls_back_to_merge_state(self) -> None:
        actions = plan([entry()], statuses={WORKTREE: WorktreeStatus(unmerged=2)})

        self.assertEqual([KEEP], [value.action for value in actions])
        self.assertIn("2 commit(s) not merged into main and no upstream branch", actions[0].reason)

    def test_a_pushed_branch_with_no_upstream_ref_is_still_removable_when_merged(self) -> None:
        actions = plan([entry()], statuses={WORKTREE: WorktreeStatus(unmerged=0)})

        self.assertEqual([REMOVE], [value.action for value in actions])
        self.assertTrue(actions[0].delete_branch)

    def test_force_removes_an_unpushed_worktree_but_keeps_its_branch(self) -> None:
        actions = plan(
            [entry()],
            statuses={WORKTREE: pushed(unmerged=2, unpushed=2)},
            force=True,
        )

        self.assertEqual([REMOVE], [value.action for value in actions])
        self.assertTrue(actions[0].forced)
        # The branch is the only copy of those commits.
        self.assertFalse(actions[0].delete_branch)


class InspectTests(unittest.TestCase):
    def runner(self, *results: CommandResult) -> mock.Mock:
        runner = mock.Mock()
        runner.run.side_effect = list(results)
        return runner

    def result(self, stdout: str = "", returncode: int = 0) -> CommandResult:
        return CommandResult(args=["git"], returncode=returncode, stdout=stdout, stderr="")

    def test_reports_dirty_state_merge_state_and_push_state(self) -> None:
        runner = self.runner(
            self.result(" M src/kanbanlan/cli.py\n"),
            self.result("2\n"),
            self.result(f"{UPSTREAM}\n"),
            self.result("1\n"),
        )

        value = inspect_worktree(runner, entry(), default_branch="main")

        self.assertEqual(
            WorktreeStatus(dirty=True, unmerged=2, unpushed=1, upstream=UPSTREAM),
            value,
        )

    def test_a_squash_merged_branch_reports_unmerged_but_fully_pushed(self) -> None:
        runner = self.runner(
            self.result(""),
            self.result("1\n"),
            self.result(f"{UPSTREAM}\n"),
            self.result("0\n"),
        )

        value = inspect_worktree(runner, entry(), default_branch="main")

        self.assertTrue(value.recoverable)
        self.assertEqual(1, value.unmerged)

    def test_the_upstream_is_read_off_the_ref(self) -> None:
        # `rev-parse refs/heads/<branch>@{upstream}` fails on a fully qualified
        # ref name, which silently reported every branch as never pushed.
        runner = self.runner(
            self.result(""),
            self.result("1\n"),
            self.result(f"{UPSTREAM}\n"),
            self.result("0\n"),
        )

        inspect_worktree(runner, entry(), default_branch="main")

        self.assertIn(
            [
                "git",
                "for-each-ref",
                "--format=%(upstream:short)",
                f"refs/heads/{BRANCH}",
            ],
            [call.args[0] for call in runner.run.call_args_list],
        )

    def test_a_branch_with_no_upstream_reports_none(self) -> None:
        runner = self.runner(
            self.result(""),
            self.result("0\n"),
            self.result(""),
        )

        value = inspect_worktree(runner, entry(), default_branch="main")

        self.assertIsNone(value.upstream)
        self.assertEqual(0, value.unpushed)
        self.assertTrue(value.recoverable)

    def test_an_uncomparable_range_counts_as_outstanding(self) -> None:
        runner = self.runner(
            self.result(""),
            self.result("", returncode=128),
            self.result("", returncode=128),
        )

        value = inspect_worktree(runner, entry(), default_branch="main")

        self.assertEqual(1, value.unmerged)
        self.assertFalse(value.recoverable)

    def test_a_detached_worktree_is_only_checked_for_dirt(self) -> None:
        runner = self.runner(self.result(" M file\n"))

        value = inspect_worktree(runner, entry(branch=None), default_branch="main")

        self.assertEqual(WorktreeStatus(dirty=True), value)


class CleanupCommandTests(unittest.TestCase):
    def run_cleanup(
        self,
        entries: list[WorktreeEntry],
        value: dict[str, Any] | None = None,
        *,
        apply: bool = False,
        force: bool = False,
        statuses: dict[str, WorktreeStatus] | None = None,
    ) -> tuple[int, str, mock.Mock]:
        provider = mock.Mock()
        store = mock.Mock()
        store.refresh.return_value = value or snapshot()
        runner = mock.Mock()
        runner.run.return_value = CommandResult(args=["git"], returncode=0, stdout=MAIN, stderr="")
        args = Namespace(
            command="cleanup",
            apply=apply,
            force=force,
            json_output=False,
            repo_root=None,
        )
        stream = StringIO()
        with (
            mock.patch(
                "kanbanlan.cli._context",
                return_value=(Path(MAIN), config(), provider, store),
            ),
            mock.patch("kanbanlan.cli.Runner", return_value=runner),
            mock.patch("kanbanlan.cli.primary_worktree", return_value=Path(MAIN)),
            mock.patch("kanbanlan.cli.list_worktrees", return_value=entries),
            mock.patch(
                "kanbanlan.cli.inspect_worktree",
                side_effect=lambda _runner, value, **_kwargs: (statuses or {}).get(
                    value.path, WorktreeStatus()
                ),
            ),
            redirect_stdout(stream),
        ):
            code = _cmd_cleanup(args)
        return code, stream.getvalue(), runner

    def git_calls(self, runner: mock.Mock) -> list[list[str]]:
        return [call.args[0] for call in runner.run.call_args_list]

    def test_planning_reports_without_removing_anything(self) -> None:
        code, output, runner = self.run_cleanup([entry()])

        self.assertEqual(2, code)
        self.assertIn(WORKTREE, output)
        self.assertNotIn(["git", "worktree", "remove", WORKTREE], self.git_calls(runner))

    def test_apply_removes_the_worktree_and_its_merged_branch(self) -> None:
        code, _, runner = self.run_cleanup([entry()], apply=True)

        self.assertEqual(0, code)
        calls = self.git_calls(runner)
        self.assertIn(["git", "worktree", "remove", WORKTREE], calls)
        self.assertIn(["git", "branch", "-d", BRANCH], calls)

    def test_apply_forces_a_dirty_worktree_and_keeps_an_unpushed_branch(self) -> None:
        code, _, runner = self.run_cleanup(
            [entry()],
            apply=True,
            force=True,
            statuses={WORKTREE: pushed(dirty=True, unmerged=1, unpushed=1)},
        )

        self.assertEqual(0, code)
        calls = self.git_calls(runner)
        self.assertIn(["git", "worktree", "remove", "--force", WORKTREE], calls)
        self.assertNotIn(["git", "branch", "-d", BRANCH], calls)
        self.assertNotIn(["git", "branch", "-D", BRANCH], calls)

    def test_apply_force_deletes_a_squash_merged_branch(self) -> None:
        code, _, runner = self.run_cleanup(
            [entry()],
            apply=True,
            statuses={WORKTREE: pushed(unmerged=1)},
        )

        self.assertEqual(0, code)
        calls = self.git_calls(runner)
        self.assertIn(["git", "worktree", "remove", WORKTREE], calls)
        self.assertIn(["git", "branch", "-D", BRANCH], calls)

    def test_apply_prunes_a_missing_directory_once(self) -> None:
        code, _, runner = self.run_cleanup(
            [entry(prunable=True), entry(path="/repo/.worktrees/other", prunable=True)],
            apply=True,
        )

        self.assertEqual(0, code)
        prunes = [
            call for call in self.git_calls(runner) if call[:3] == ["git", "worktree", "prune"]
        ]
        self.assertEqual(1, len(prunes))

    def test_a_board_with_nothing_to_clean_succeeds(self) -> None:
        value = snapshot([item(state="OPEN", status="In progress", claim={"session": "claude-1"})])

        code, output, runner = self.run_cleanup([entry()], value)

        self.assertEqual(0, code)
        self.assertIn("claude-1", output)
        self.assertNotIn(["git", "worktree", "remove", WORKTREE], self.git_calls(runner))


class CleanupParserTests(unittest.TestCase):
    def test_cleanup_plans_by_default(self) -> None:
        args = build_parser().parse_args(["cleanup"])

        self.assertEqual("cleanup", args.command)
        self.assertFalse(args.apply)
        self.assertFalse(args.force)

    def test_cleanup_accepts_apply_and_force(self) -> None:
        args = build_parser().parse_args(["cleanup", "--apply", "--force"])

        self.assertTrue(args.apply)
        self.assertTrue(args.force)


if __name__ == "__main__":
    unittest.main()
