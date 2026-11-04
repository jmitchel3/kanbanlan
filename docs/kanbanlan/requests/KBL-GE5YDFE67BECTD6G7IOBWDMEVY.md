# Add start, done, and drop verbs and hide internals under admin

- Kanbanlan: `KBL-GE5YDFE67BECTD6G7IOBWDMEVY`
- Canonical home: `github`
- Canonical request: [#103](https://github.com/jmitchel3/kanbanlan/issues/103)

## Request

kanbanlan start "<intent or ref>" does overlap check, triage-if-needed, claim, branch, worktree, and prunes orphaned worktrees in one call. done opens or links the PR and moves to review. drop releases or closes. Internal commands move under kanbanlan admin with the old names kept as hidden aliases.

## Decisions

- The verbs compose the existing commands in-process. `_step_args` parses a
  step's argv with the real parser, and a `result_sink` on the namespace makes
  `_emit_result` hand the step's JSON result back instead of printing it. Every
  rule (local-first planning, arbitration, live re-validation on sync) is the
  one the underlying command applies, and `--json` for existing commands is
  unchanged.
- Admin commands are registered twice from one function: listed under
  `kanbanlan admin`, and unlisted at the top level (a subparser added without
  `help` is left out of the command list, and the subparser `metavar` shows only
  the visible names). Queued sync changes replay as `kanbanlan claim ...` and
  similar, so the top-level names must keep working.
- `start` checks hard refusals (claimed by another session, session already
  owns a different request) before anything is created, so a refused start
  never leaves a captured card behind. Starting the request the session already
  owns prints its worktree again instead of refusing.
- Worktree pruning finds candidates from the local view with no fetch and no
  GitHub read. Only when something looks removable does it decide again exactly
  as `cleanup --apply` does (fresh board read, fetched default branch), with
  queued changes laid over the board so a claim made moments ago keeps its
  worktree. Cleanup now inspects only worktrees it could remove, in parallel.
- `done` pushes the branch and opens the pull request only when no open one
  exists for the branch, then retries `review` briefly while GitHub links the
  new pull request's closing reference.

## Verification

- `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv build`.
- `tests/test_verbs.py` covers help listing, hidden aliases, admin dispatch,
  start (capture, triage, claim, refusals, resume, overlap report, pruning),
  done (create, reuse, link retry), drop (release, close), and the PR body.
- Latency, warm cache, this repository (4 worktrees), measured in-process
  against a copy of the live cache with the sync drainer disabled: full `start`
  of a new title took 1.63 to 1.85 s across three runs. `claim` (git fetch of
  the default branch plus `git worktree add`) was about 1.1 to 1.2 s of that;
  overlap, capture, and triage were under 0.15 s each.

## Delivered result

- New `start`, `done`, and `drop` verbs; top-level help lists only init,
  start, done, drop, status, next, doctor, and admin.
- `kanbanlan admin` holds every other command; the old top-level names remain
  as unlisted aliases.
- README documents the four verbs and the admin group. The managed
  AGENTS.md/CLAUDE.md templates and docs/workflow are left for card #105.
- Follow-up outside this card: the repository snapshot omitted #103 (on
  Project 2, not archived) even after a full refresh, so `record #103` and
  the session-ownership check could not see it from the cache.
