"""Planning for the worktrees `claim` creates and `cleanup` removes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kanbanlan.domain import KanbanlanRequest
from kanbanlan.identity import KANBANLAN_ID_PATTERN
from kanbanlan.runner import Runner

# `claim` names both the branch and the directory `<identity>-<slug>`, where
# identity is the lowercased Kanbanlan ID, so either one links a worktree back
# to its request without reading claim history.
IDENTITY_RE = re.compile(rf"(?P<id>{KANBANLAN_ID_PATTERN})", re.IGNORECASE)

REMOVE = "remove"
PRUNE = "prune"
KEEP = "keep"


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    branch: str | None = None
    detached: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass(frozen=True)
class WorktreeStatus:
    """What the working tree and its branch still hold."""

    dirty: bool = False
    unmerged: int = 0


@dataclass(frozen=True)
class CleanupAction:
    path: str
    action: str
    reason: str
    branch: str | None = None
    kanbanlan_id: str | None = None
    provider_ref: str | None = None
    delete_branch: bool = False
    forced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "branch": self.branch,
            "kanbanlan_id": self.kanbanlan_id,
            "provider_ref": self.provider_ref,
            "delete_branch": self.delete_branch,
            "forced": self.forced,
        }


def parse_worktree_list(output: str) -> list[WorktreeEntry]:
    """Parse `git worktree list --porcelain` into entries."""

    entries: list[WorktreeEntry] = []
    current: dict[str, Any] = {}

    def flush() -> None:
        if current.get("path"):
            entries.append(
                WorktreeEntry(
                    path=current["path"],
                    branch=current.get("branch"),
                    detached=current.get("detached", False),
                    locked=current.get("locked", False),
                    prunable=current.get("prunable", False),
                )
            )
        current.clear()

    for line in output.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            flush()
            current["path"] = value
        elif key == "branch":
            # Only refs/heads/<name> can be checked out in a worktree.
            current["branch"] = value.removeprefix("refs/heads/")
        elif key in {"detached", "locked", "prunable"}:
            current[key] = True
    flush()
    return entries


def worktree_identity(entry: WorktreeEntry) -> str | None:
    """Return the Kanbanlan ID a worktree's branch or directory names."""

    for candidate in (entry.branch, Path(entry.path).name):
        if not candidate:
            continue
        match = IDENTITY_RE.search(candidate)
        if match:
            return match.group("id").upper()
    return None


def list_worktrees(runner: Runner) -> list[WorktreeEntry]:
    return parse_worktree_list(runner.run(["git", "worktree", "list", "--porcelain"]).stdout)


def inspect_worktree(
    runner: Runner,
    entry: WorktreeEntry,
    *,
    default_branch: str,
) -> WorktreeStatus:
    """Report uncommitted work and commits the default branch does not have."""

    dirty = bool(runner.run(["git", "-C", entry.path, "status", "--porcelain"]).stdout.strip())
    unmerged = 0
    if entry.branch:
        result = runner.run(
            [
                "git",
                "rev-list",
                "--count",
                f"origin/{default_branch}..refs/heads/{entry.branch}",
            ],
            check=False,
        )
        # A branch with no upstream comparison available is treated as
        # unmerged, because doubt must never delete work.
        unmerged = int(result.stdout.strip() or 0) if result.returncode == 0 else 1
    return WorktreeStatus(dirty=dirty, unmerged=unmerged)


def plan_cleanup(
    entries: list[WorktreeEntry],
    snapshot: dict[str, Any],
    statuses: dict[str, WorktreeStatus],
    *,
    main_path: str,
    current_path: str,
    default_branch: str,
    force: bool = False,
) -> list[CleanupAction]:
    """Decide what happens to every linked worktree, mutating nothing."""

    requests = {
        item["kanbanlan_id"]: item
        for item in snapshot.get("items", [])
        if item.get("type") == "ISSUE" and item.get("kanbanlan_id")
    }
    actions: list[CleanupAction] = []
    for entry in entries:
        if entry.path == main_path:
            continue
        identity = worktree_identity(entry)
        item = requests.get(identity) if identity else None
        provider_ref = item.get("provider_ref") if item else None

        def action(
            kind: str,
            reason: str,
            *,
            delete_branch: bool = False,
            forced: bool = False,
        ) -> CleanupAction:
            return CleanupAction(
                path=entry.path,
                action=kind,
                reason=reason,
                branch=entry.branch,
                kanbanlan_id=identity,
                provider_ref=provider_ref,
                delete_branch=delete_branch,
                forced=forced,
            )

        if entry.prunable:
            actions.append(action(PRUNE, "worktree directory is already gone"))
            continue
        if entry.path == current_path:
            actions.append(action(KEEP, "current worktree"))
            continue
        if entry.locked:
            actions.append(action(KEEP, "worktree is locked"))
            continue
        if item is None:
            actions.append(action(KEEP, "not linked to a request on this board"))
            continue
        claim = item.get("active_claim") or {}
        if claim:
            owner = claim.get("session") or "another session"
            actions.append(action(KEEP, f"still claimed by {owner}"))
            continue

        status = statuses.get(entry.path, WorktreeStatus())
        request = KanbanlanRequest.from_snapshot_item(item)
        settled = "request is closed" if request.state == "CLOSED" else "claim was released"
        if status.dirty and not force:
            actions.append(action(KEEP, "uncommitted changes; rerun with --force to discard"))
            continue
        if status.unmerged and not force:
            actions.append(
                action(
                    KEEP,
                    f"{status.unmerged} commit(s) not merged into {default_branch}; "
                    "rerun with --force to remove the worktree anyway",
                )
            )
            continue
        forced = bool(status.dirty or status.unmerged)
        actions.append(
            action(
                REMOVE,
                settled,
                # An unmerged branch is the only copy of its commits, so the
                # branch outlives the worktree even under --force.
                delete_branch=not status.unmerged,
                forced=forced,
            )
        )
    return actions


def format_action(value: CleanupAction) -> str:
    label = value.provider_ref or value.kanbanlan_id or "unlinked"
    detail = f"{value.action} {value.path} ({label}): {value.reason}"
    if value.action == REMOVE and value.delete_branch and value.branch:
        detail = f"{detail}; deletes branch {value.branch}"
    return detail
