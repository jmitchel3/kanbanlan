# Worker should refresh each repository and Project once per cycle

- Kanbanlan: `KBL-BM5UPFJINBAETF5GV4BENQPEC4`
- Canonical home: `github`
- Canonical request: [#86](https://github.com/jmitchel3/kanbanlan/issues/86)

## Request

Observed 2026-09-28: the registry held paracord-clients/rangertrac.org twice (the live checkout and a stale clone at ~/Dev/paracord-clients/rangertrac.org, 1493 commits behind), so the worker fully refreshed the same 1611-item Project twice every 5 minutes. prevenir-automations and prevenircardiowell.com also share one Project and refresh it separately. Unbound repositories bill the active gh account, so all of this landed on one account's 5000-point pool (4794 used in an hour).

Outcome: the worker groups registrations by repository and by Project and refreshes each once per cycle; duplicate registrations of one repository are reported by 'worker status' and 'doctor' (and a registration whose root is behind origin or unused is flagged). 'worker status' shows the GraphQL points each registration spent in the last cycle.

Also observed: 8 'kanbanlan worker run' processes were alive at once (one holding worker.lock, one running from a feature worktree's venv). 'worker start' should detect a live worker and not launch another, and idle waiters should exit.

## Decisions

- Root cause of the eight live `worker run` processes: `ps -o etimes=` is a
  procps extension that macOS rejects, so every lock owner's age was
  unverifiable there. After six hours (`UNVERIFIABLE_OWNER_CAP_SECONDS`) a
  live worker's lock was judged stale, `worker status` or `start` swept it,
  and a second worker started while the first kept looping. `locks.py` now
  falls back to the POSIX `etime` form (`[[dd-]hh:]mm:ss`), and the running
  worker touches its lock after every sleep so it never ages past the cap
  even where no age is readable.
- The running worker re-checks lock ownership after every sleep. If another
  live worker holds the lock it exits; if the lock was swept and is free it
  reacquires it and keeps running, so a sweep never leaves zero workers.
- Duplicate registrations are grouped by `hostname/repository`
  (case-insensitive). The serviced one is chosen by: root is a Git checkout,
  then newest Git activity (mtimes of `HEAD`, `index`, `FETCH_HEAD`,
  `logs/HEAD` in the common directory and each linked worktree), then newest
  `last_success_at`. The registry is not rewritten; duplicates stay visible
  until someone removes or disables them.
- Project reuse is not safe: repository snapshots are repository-scoped (each
  paginates the whole Project and keeps only its own repository's content).
  The fallback is at most one refresh per Project per cycle, rotating through
  sharing repositories by oldest `last_run_at`. Only a successful refresh
  claims the Project for the cycle, so a failing repository never starves its
  sibling. Cost: each sharing repository is reconciled every N intervals
  instead of every interval.
- "Behind origin" was not implemented; it needs a `git fetch` or a possibly
  stale remote ref per registration. `worker status` reports
  `last_activity_at` instead, which identifies an unused clone.
- GraphQL points are measured by wrapping the scoped runner
  (`GraphQLPointMeter`) and summing `data.rateLimit.cost` from every
  `gh api graphql` response, counting one point for responses with no cost
  (mutations). REST calls such as `gh issue list` are not counted. Stored as
  `Registration.last_graphql_points`; older registries load with `None`.

## Verification

- `uv run pytest -q`: 439 passed, 40 subtests passed.
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- New tests in `tests/test_worker.py` cover duplicate selection by activity,
  missing and non-Git roots, shared-Project rotation across two cycles,
  failed refresh not deferring a sibling, point metering end to end, lock
  touch and takeover detection, exit on a live successor, reclaiming a swept
  lock, `start` not launching beside a live worker, and the `etime` fallback.
- Confirmed on macOS that `ps -o etimes=` fails and the fallback returns the
  real elapsed time.
- `kanbanlan worker status` against a temporary `KANBANLAN_STATE_DIR` with a
  duplicate registration reported `duplicate_repository` and `missing_root`.

## Delivered result

The worker services each repository once per cycle from its most active
clone, refreshes a shared Project at most once per cycle, records GraphQL
points per registration, and runs as a single instance. `worker status`
reports `problems`, `root_state`, `last_activity_at`, `duplicate_skipped`,
and `last_graphql_points`; `doctor` warns about registry problems. See
`docs/workflow/worker.md`.

Follow-up: a "behind origin" flag, and a command to remove a stale
registration (today it can only be disabled from inside its checkout).
