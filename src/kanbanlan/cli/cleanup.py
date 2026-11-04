"""Remove request worktrees that are no longer claimed."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from kanbanlan.cli.context import _context
from kanbanlan.cli.output import _emit_result
from kanbanlan.config import Config, primary_worktree
from kanbanlan.runner import Runner
from kanbanlan.ui import status, success, warning
from kanbanlan.worktrees import (
    PRUNE,
    CleanupAction,
    format_action,
    inspect_worktree,
    list_worktrees,
    needs_inspection,
    plan_cleanup,
    removals,
    settled_requests,
)


def _cmd_cleanup(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    runner = Runner(root)
    with status("Loading worktrees and current claim state"):
        snapshot = store.refresh(provider)
        plan = _plan_worktree_cleanup(root, config, runner, snapshot, force=args.force)
    pending_removals = removals(plan)
    payload = [value.to_dict() for value in plan]
    if not pending_removals:
        if _emit_result(args, {"applied": False, "plan": payload, "removed": []}):
            return 0
        for value in plan:
            print(format_action(value))
        success("No worktree needs cleanup.")
        return 0
    if args.json_output and not args.apply:
        _emit_result(args, {"applied": False, "plan": payload, "removed": []})
        return 2
    if not args.json_output:
        for value in plan:
            print(format_action(value))
    if not args.apply:
        warning(
            f"{len(pending_removals)} worktree(s) to clean up; rerun with --apply to remove them."
        )
        return 2
    removed = _apply_cleanup(runner, pending_removals)
    if _emit_result(args, {"applied": True, "plan": payload, "removed": removed}):
        return 0
    success(f"Cleaned up {len(removed)} worktree(s).")
    return 0


def _plan_worktree_cleanup(
    root: Path,
    config: Config,
    runner: Runner,
    snapshot: dict[str, Any],
    *,
    force: bool,
    fetch: bool = True,
) -> list[CleanupAction]:
    """Plan what 'cleanup' does to each linked worktree against ``snapshot``."""

    entries = list_worktrees(runner)
    main_path = str(primary_worktree(root))
    current_path = runner.run(
        ["git", "rev-parse", "--path-format=absolute", "--show-toplevel"]
    ).stdout.strip()
    if fetch:
        # Merge state is only meaningful against a current default branch.
        runner.run(["git", "fetch", "origin", config.default_branch])
    settled = settled_requests(snapshot)
    inspected = [
        entry
        for entry in entries
        if entry.path not in {main_path, current_path} and needs_inspection(entry, settled)
    ]
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(inspected)))) as pool:
        results = pool.map(
            lambda entry: inspect_worktree(runner, entry, default_branch=config.default_branch),
            inspected,
        )
        statuses = {entry.path: value for entry, value in zip(inspected, results, strict=True)}
    return plan_cleanup(
        entries,
        snapshot,
        statuses,
        main_path=main_path,
        current_path=current_path,
        default_branch=config.default_branch,
        force=force,
    )


def _apply_cleanup(runner: Runner, removals: list[CleanupAction]) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    pruned = False
    for value in removals:
        if value.action == PRUNE:
            if not pruned:
                with status("Pruning worktree entries whose directory is gone"):
                    runner.run(["git", "worktree", "prune"])
                pruned = True
            removed.append(value.to_dict())
            continue
        with status(f"Removing worktree {value.path}"):
            command = ["git", "worktree", "remove", value.path]
            if value.forced:
                command.insert(3, "--force")
            runner.run(command)
            if value.delete_branch and value.branch:
                flag = "-D" if value.force_delete_branch else "-d"
                runner.run(["git", "branch", flag, value.branch])
        removed.append(value.to_dict())
    return removed
