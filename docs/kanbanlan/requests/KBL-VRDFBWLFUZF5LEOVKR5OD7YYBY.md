# Cleanup keeps squash-merged worktrees because it tests the wrong branch state

- Kanbanlan: `KBL-VRDFBWLFUZF5LEOVKR5OD7YYBY`
- Canonical home: `github`
- Canonical request: [#61](https://github.com/jmitchel3/kanbanlan/issues/61)

## Request

## Outcome

`kanbanlan cleanup` removes the worktree of a request whose branch was
squash-merged, without `--force`.

## Problem

`inspect_worktree` measures delivery as
`rev-list --count origin/<default>..<branch>`. A squash merge rewrites the
commit, so a fully delivered branch still reports commits "not merged" and the
worktree is kept. This repository squash-merges every pull request, so the
command is effectively `--force`-only for its primary case. Observed on
`work/kbl-spkwmkubb5bxzji23ft3uwghgm-...` immediately after #60 merged: one
commit not merged into `main`, zero commits unpushed.

## Scope

- Gate on unpushed commits (`<upstream>..<branch>`) rather than
  unmerged-into-default. A pushed commit survives on its pull request even after
  the branch is deleted, so nothing unique is lost.
- Keep the unmerged-into-default measure as the fallback for a branch with no
  upstream, where the local branch is the only copy.
- Delete the local branch when it is either merged into the default branch or
  fully pushed, using `-D` only in the pushed-but-rewritten case. Never delete a
  branch that still holds unpushed commits, even under `--force`.
- Reasons name which check kept the worktree.
- Tests and documentation.

## Decisions

- The question a cleanup gate must answer is not "was this branch merged" but
  "does this worktree hold a commit that exists nowhere else". Merge state
  answered the wrong question: a squash merge writes a new commit onto the
  default branch, so a fully delivered branch keeps reporting commits the
  default branch does not contain. Every pull request in this repository is
  squash-merged, so `cleanup` was `--force`-only for its primary case from the
  moment it shipped.
- `WorktreeStatus.recoverable` is the new gate: a branch with an upstream is
  settled once nothing is unpushed, because a pushed commit stays on the remote
  and on the pull request that carried it even after the branch is deleted. A
  branch that was never pushed has no such copy, so merge state still decides
  there. Uncommitted changes keep a worktree either way.
- Branch deletion follows the same rule rather than a second one. `-d` is used
  when the default branch actually contains the commits; `-D` only for a branch
  that is fully pushed but rewritten, which is exactly the squash case. A branch
  holding unpushed commits is never deleted, including under `--force`, so
  `--force` still discards only uncommitted changes.
- The upstream is read with
  `git for-each-ref --format=%(upstream:short) refs/heads/<branch>`, not
  `git rev-parse --abbrev-ref refs/heads/<branch>@{upstream}`. The `rev-parse`
  form fails with "no such branch" on a fully qualified ref name, and because
  the lookup treats failure as "no upstream", every branch silently fell back to
  merge state. A test now pins the exact command.
- The keep reason names the measure that applied, so the two cases read
  differently: "N commit(s) not pushed to <upstream>" versus "N commit(s) not
  merged into <default> and no upstream branch".

## Verification

- `uv run pytest -q`: 337 passed, 37 subtests. `tests/test_cleanup.py` grows to
  36 tests, adding the squash-merged branch (removable, `-D` deletion), a
  fast-forward branch (`-d` deletion), unpushed commits keeping a worktree, the
  no-upstream fallback to merge state, forced removal keeping an unpushed
  branch, the detached-worktree path, the uncomparable-range path, and the
  `for-each-ref` command guard.
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- Live verification in this repository, all on scratch worktrees named for
  closed request #55:
  - a branch pushed and then squash-merged in shape (one commit not on `main`,
    zero unpushed) planned as `remove ... deletes branch ...` without `--force`,
    and `--apply` removed the worktree and deleted the branch;
  - the first run of that same case before the `for-each-ref` fix reported
    "1 commit(s) not merged into main and no upstream branch", which is how the
    `rev-parse` defect was found;
  - a branch with one unpushed commit was kept with
    "1 commit(s) not pushed to origin/...", and `--force` removed the worktree
    while leaving the branch and its unpushed commit intact.

## Delivered result

`cleanup` now decides on recoverability rather than merge state, so a
squash-merged worktree is removed without `--force` and its branch is deleted
with `-D`, while a branch holding unpushed commits keeps its worktree and
survives even a forced removal. The upstream lookup that the whole gate depends
on is read off the ref and pinned by a test.
