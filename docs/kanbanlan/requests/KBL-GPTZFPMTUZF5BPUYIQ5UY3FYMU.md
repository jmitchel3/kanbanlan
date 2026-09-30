# Let queued writes proceed during a refresh cooldown

- Kanbanlan: `KBL-GPTZFPMTUZF5BPUYIQ5UY3FYMU`
- Canonical home: `github`
- Canonical request: [#83](https://github.com/jmitchel3/kanbanlan/issues/83)

## Request

Observed 2026-09-28 in paracord-clients/rangertrac.org: when the snapshot's recorded GraphQL remaining fell below rate_limit_floor, check_refresh_allowed made every queued capture/triage fail instantly with 'GitHub refresh deferred until <reset> to preserve quota'. Ten writes failed this way in one minute (22:32Z), though each costs only a few points.

Outcome: a lifecycle write (capture, triage, claim, handoff, close) executes during a cooldown; only the follow-up snapshot refresh is deferred, and cached state is marked stale until the refresh runs. A write that GitHub itself refuses for rate limiting still fails with the upstream reset time.

Out of scope: retrying failed intents (separate card).

## Decisions

- The cooldown gate (`check_refresh_allowed`) still guards every full-board
  read, including `read_board`, reconciliation, `kanbanlan refresh`, and the
  worker. The quota floor keeps protecting those reads unchanged.
- A local deferral is marked with `RateLimitError.deferred = True` instead of
  a subclass, so the stable JSON error `kind` and the worker's persisted
  `RateLimitError` cooldown matching keep working.
- `CacheStore.refresh_for_write` is `refresh`, except that a local deferral
  serves the last usable snapshot and invalidates it, so the first read after
  the reset fetches the board. It still raises when GitHub itself refuses the
  read (upstream reset time preserved) or when no usable snapshot exists.
- The live paths of `triage`, `review`, `release`, `close`, and `handoff`, the
  foreground `_refresh_after_mutation`, and the outbox drainer's post-batch
  refresh use `refresh_for_write`. Their snapshot is only needed for item ids,
  Project field ids, and preconditions the queued intent was already planned
  against. Capture already avoids board reads (#89).
- `claim` keeps the strict `refresh`: its post-comment read is the only check
  that a concurrent claim did not win, and a cached snapshot cannot answer
  that. During a deferral it still fails before posting anything, so no
  orphaned `CLAIM` comment is left. A targeted verification read (#92) is
  what lets claim proceed during a cooldown.
- `check_refresh_allowed` no longer rewrites health during an active
  cooldown, and `read_board` no longer re-records a local deferral, so
  repeated checks never replace GitHub's own refusal message or move its
  reset time. A quota-floor deferral is still written to health once, with
  the snapshot's reset, so `status` shows why refreshing stopped.

## Verification

- `uv run pytest -q`: 433 passed.
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- New tests: a write-path read serves the cached snapshot during a floor
  deferral without fetching and leaves it stale until an `ensure` after the
  reset fetches; GitHub's own refusal still raises with the upstream reset;
  a deferral with no usable snapshot still fails; repeated deferrals leave the
  recorded cooldown byte-identical and the refresh resumes right after the
  original reset; `read_board` keeps GitHub's refusal on record; `close` runs
  end to end against a real `CacheStore` below the floor, writing to GitHub
  without any board read. The new tests fail without the source change.

## Delivered result

Lifecycle writes for triage, review, release, close, and handoff, both live
and replayed by the outbox drainer, now execute during a refresh cooldown;
only full-board refreshes are deferred, and the cached snapshot is marked
stale until one runs. Follow-ups: `claim` needs the targeted verification
read from #92 to proceed during a cooldown, and the worker (#86) still checks
the cooldown before draining the outbox, so it leaves queued intents for the
session drainer until the reset.
