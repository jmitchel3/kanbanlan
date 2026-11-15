# start spends minutes pruning orphaned worktrees

- Kanbanlan: `KBL-7KIN5HBAIRARHGCKI6T4CYLVUE`
- Canonical home: `github`
- Canonical request: [#125](https://github.com/jmitchel3/kanbanlan/issues/125)
- Pull request: [#128](https://github.com/jmitchel3/kanbanlan/pull/128)

## Request

start took 100s to over 3 minutes in repos with many closed-request worktrees (dash.paracord.co pruned 23) because pruning runs serially before the claim. Pruning must not sit on the start critical path: bound it, parallelise it, or do it after the claim returns, keeping cleanup's safety rules.

## Decisions

- Profiling (synthetic repo, 25 orphaned worktrees with 2,000 ignored files each, fake GitHub at 1.5 s per board read) showed the post-claim prune cost: a second full board read (1.5 s), a fetch plus re-inspection of every candidate (0.57 s), and serial `git worktree remove` of all 25 (4.5 s). Removal time scales with the files in each checkout, so it dominates in repos with dependencies and build output.
- `start` now settles prune candidates against the live claim read it already made, so pruning costs no second board read. Without a session (no live read) it still reads the board once before removing anything.
- The claim already fetches the default branch, so the confirming plan skips its own fetch. A stale `origin/main` can only overcount unmerged commits, which keeps more, never less.
- `start` removes at most `START_PRUNE_LIMIT` (3) worktrees and re-inspects only those; missing directories are always included since one `git worktree prune` clears them. The rest are reported as `cleanup_deferred` (JSON) and a warning pointing at `kanbanlan admin cleanup --apply`.
- The request just claimed is excluded by Kanbanlan ID and worktree path, because the live read predates its claim and would show it unclaimed.
- `_plan_worktree_cleanup` gains `paths` to limit planning and inspection; `admin cleanup` is unchanged. The safety rules (never remove a worktree with uncommitted or unpushed work) are the same `plan_cleanup` decisions as before.

## Verification

- [x] `uv run pytest` (515 passed)
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv build`
- New tests (fail before the change): the live read serves both the claim check and the confirmation, the confirmation skips the fetch and is limited to chosen paths, removals are bounded with the rest deferred, the just-claimed request is never pruned, and `paths` limits planning and inspection.
- Benchmark, `start` end to end with a fake claim (1.0 s) and fake GitHub (1.5 s per read), 25 orphaned worktrees:
  - 2,000 files each: before 8.8 s (3 board reads, 25 removed), after 2.3 s (2 board reads, 3 removed, 22 deferred).
  - 10,000 files each: before 24.2 s, after 4.0 s.

## Delivered result

`start` returns the claimed worktree promptly regardless of how many orphaned worktrees exist; the bulk of cleanup is left to `kanbanlan admin cleanup --apply`, which `start` now names when it defers work.
