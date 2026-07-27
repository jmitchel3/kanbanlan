# Add opt-in background reconciliation worker

- Kanbanlan: `KBL-C4MZNCQUGZDEFKR2OQTBJS3DQA`
- Canonical home: `github`
- Canonical request: [#5](https://github.com/jmitchel3/kanbanlan/issues/5)

## Request

Keep every locally registered Kanbanlan repository reconciled in the background
through one tiny user-level worker, without requiring a terminal or a worker per
worktree.

## Activation

A repository becomes registered and enabled after a successful live `kanbanlan
init` that performs reconciliation, or a successful `kanbanlan reconcile` /
`kanbanlan reconcile --apply`. Local-only setup, skipped reconciliation, failed
initialization, unresolved drift, and explicit disablement do not activate it.

## Acceptance criteria

- [x] One user-scoped worker services all enabled local repositories and
      deduplicates Git worktrees through the Git common directory.
- [x] The worker periodically refreshes live state, plans reconciliation,
      applies safe repairs, and verifies the result. GitHub remains canonical.
- [x] The worker uses repository-specific GitHub authentication without
      switching the globally active `gh` account.
- [x] Concurrent runs are prevented with PID-aware locking; transient failures
      preserve the last good snapshot and use bounded retry/backoff with health
      metadata.
- [x] Commands expose worker and repository state, including status, enable,
      disable, start, and stop behavior.
- [x] Initialization or reconciliation can activate the worker automatically,
      while explicit disablement is respected.
- [x] The implementation is lightweight, has no Docker requirement, and
      documents macOS LaunchAgent and Linux systemd user-service behavior.
- [x] Tests cover registry/worktree deduplication, worker locking, auth
      isolation, failures/backoff, health, disablement, and lifecycle parsing.

## Decisions

- The registry is user-scoped, atomically written with mode 0600, and keyed by
  the resolved Git common directory. Worktrees update one registration rather
  than creating duplicate workers.
- Credentials are never persisted. The registry stores the selected GitHub host
  and account; each run resolves that account with `gh auth token --user` or a
  scoped token environment variable, then passes `GH_HOST` and `GH_TOKEN` only
  to its subprocesses.
- A PID-aware worker lock prevents concurrent iterations and does not steal a
  live process merely because a lock file is old. Per-repository refresh locks
  continue to protect atomic snapshots.
- Duplicate identities and other unresolved unsafe drift are recorded as a
  health error and skipped rather than repeatedly applying unsafe mutations.
- Backoff is 30 seconds doubled per failure and capped at one hour. Successful
  verification resets the failure counter and retry timestamp.

## Verification

- `uv run pytest -q` — 82 tests and 4 subtests passed.
- `uv run ruff check .` — passed.
- `uv run ruff format --check .` — all files formatted.
- `git diff --check` — passed.
- Registry tests cover atomic permissions, common-directory deduplication,
  persistent disablement, and health round trips.
- Worker tests cover PID locking, disabled repositories, scoped auth env,
  success/reset behavior, failure/backoff health, and status output.
- CLI tests cover worker lifecycle parsing and disablement activation gates.

## Delivered result

Kanbanlan now has an opt-in user-level reconciliation worker with a durable
registry, worktree deduplication, scoped GitHub authentication, PID-aware
locking, bounded retries, health reporting, lifecycle commands, activation
gates, and macOS/Linux service documentation. The worker preserves last-good
snapshots during transient failures and leaves unresolved unsafe drift for
explicit operator review.
