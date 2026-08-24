# Add a cleanup command for orphaned request worktrees

- Kanbanlan: `KBL-SPKWMKUBB5BXZJI23FT3UWGHGM`
- Canonical home: `github`
- Canonical request: [#59](https://github.com/jmitchel3/kanbanlan/issues/59)

## Request

## Outcome

`kanbanlan cleanup` reports, and with `--apply` removes, the worktrees and
branches that `claim` created for requests that are no longer claimed, so the
lifecycle that starts with a checkout also ends one.

`claim` creates a dedicated worktree and branch per request and nothing ever
removes them. Merged and abandoned work accumulates on disk, and a stale
worktree also holds its branch, so `gh pr merge --delete-branch` fails.

## Scope

- `cleanup` plans first and mutates only under `--apply`, matching `reconcile`
  and `rehome`.
- Links each linked worktree to a request through the `work/<identity>-<slug>`
  branch and directory convention that `claim` writes.
- Removes only worktrees whose request has no active claim. Refuses on
  uncommitted changes or commits not merged into the default branch, both
  overridable with `--force`, which still never deletes an unmerged branch.
- Deletes the local branch only when it is fully merged, and prunes worktree
  administrative entries whose directory is already gone.
- Never touches the primary worktree, the current worktree, or a worktree it
  cannot link to a request.
- Tests and README plus generated workflow documentation.

## Decisions

- A worktree is linked to its request through the Kanbanlan ID that `claim`
  already writes into both the branch (`work/<identity>-<slug>`) and the
  directory name, rather than through claim history. `active_claim` only
  reports the claim that is still open, and past CLAIM comments (which carry
  `Branch:` and `Worktree:`) are not in the snapshot, so reading history would
  have meant a snapshot schema change to recover what the names already encode.
  The cost is that a worktree created with an explicit `--branch` that omits
  the identity cannot be linked; it is reported and never removed.
- Removal requires only that the request has no active claim, not that it is
  closed. A released or handed-back card leaves the same orphan behind, and the
  reason line distinguishes `request is closed` from `claim was released`.
- Planning is pure. `plan_cleanup` takes parsed worktree entries, the snapshot,
  and a map of already-collected working-tree statuses, so every decision is
  testable without a git repository; `list_worktrees` and `inspect_worktree`
  hold the git calls behind an injectable `Runner`.
- Merge state is measured as `rev-list --count origin/<default>..<branch>`
  after a fetch, which subsumes "unpushed" (an unpushed commit is by definition
  not in the remote default branch). A branch that cannot be compared at all
  counts as one unmerged commit, so an unexpected git failure keeps the
  worktree rather than removing it.
- `--force` removes a worktree with uncommitted changes or unmerged commits,
  but the branch is deleted only when the branch is fully merged, and with
  `git branch -d` rather than `-D`. Under `--force` the commits therefore stay
  recoverable from the branch; only uncommitted changes are actually discarded,
  which is what the flag exists to opt into.
- The primary worktree is skipped silently. The current worktree, a locked
  worktree, and any worktree that cannot be linked to a request are reported as
  kept, so the plan stays a complete inventory of what exists.
- A worktree whose directory is already gone is pruned through a single
  `git worktree prune`, and its branch is left alone: the directory carries no
  signal about whether the branch still holds work.
- The command follows `reconcile`: plan by default with exit status 2 when
  there is work, mutate only under `--apply`, and emit the same plan in
  `--json`.

## Verification

- `uv run pytest -q`: 330 passed, 37 subtests. `tests/test_cleanup.py` adds 28
  tests covering porcelain parsing (branch prefix, detached, locked, prunable,
  and a final entry with no trailing blank line), identity resolution from
  branch and from directory name, every plan branch (primary, current, locked,
  unlinked, other-request, active claim, closed, released, dirty, unmerged, and
  both `--force` paths), `inspect_worktree` against a fake runner including the
  uncomparable-branch case, and the command itself (plan-only exit 2, apply
  removing the worktree and its merged branch, forced removal keeping an
  unmerged branch, a single prune for two missing directories, and a clean
  board).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- Live verification in this repository, from the request worktree:
  - a scratch worktree named for closed request #55 planned as
    `remove ... request is closed; deletes branch ...`, and `--apply` removed
    the directory and deleted the merged branch;
  - the same worktree with one untracked file planned as
    `keep ... uncommitted changes; rerun with --force to discard`;
  - the current worktree and the unlinked `.worktrees/release-0.9.0` were both
    reported as kept, and `release-0.9.0` was left untouched;
  - a worktree whose directory was deleted by hand planned as `prune` and was
    cleared by a single `git worktree prune`.

## Delivered result

`kanbanlan cleanup` reports every linked worktree and, under `--apply`, removes
the ones whose request no longer has an active claim, deleting the local branch
when it is fully merged and pruning administrative entries whose directory is
gone. Uncommitted changes and unmerged commits keep a worktree unless `--force`
is given, and an unmerged branch always outlives its worktree. `claim` now has
the closing half of its lifecycle.

Follow-up worth considering: a worktree created with an explicit `--branch` that
drops the Kanbanlan ID stays unlinked forever. Recording the claim's worktree
path in the snapshot would close that gap, at the cost of a schema addition.
