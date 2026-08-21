# Snapshot refresh degrades gracefully under GitHub rate limits

- Kanbanlan: `KBL-2FFYGZHCYNEPXPEU5D6TIHV7GM`
- Canonical home: `github`
- Canonical request: [#44](https://github.com/jmitchel3/kanbanlan/issues/44)

## Request

Snapshot refresh stops stampeding and stops hard-failing when the GitHub
GraphQL point budget runs low.

- `CacheStore.ensure` re-checks freshness inside the refresh lock so concurrent
  sessions reuse one fetch.
- `GitHub.graphql` raises a typed `RateLimitError` (with `resetAt` when
  reported) for `RATE_LIMITED` and secondary-rate-limit failures.
- `ensure` serves the last-good snapshot with `refresh_status: "throttled"`
  health when refresh is rate limited; only a missing snapshot makes it fatal.
- `ensure` defers refresh while the cached snapshot's `rate_limit.remaining` is
  below a floor and `resetAt` has not passed.
- `status` and `doctor` surface remaining, `resetAt`, and throttled state.

See `docs/improvements/by-fable.md` (request 1).

## Decisions

- `RateLimitError` lives in `runner.py` beside the transient-failure markers so
  both `github.py` (which raises it) and `snapshot.py` (which catches it) can
  import it without a cycle. It deliberately does not join `TRANSIENT_MARKERS`:
  a rate-limited call keeps failing until the quota window resets, so retrying
  is pure waste.
- Rate-limit failures are detected on both paths `gh api graphql` can take: a
  non-zero exit whose output matches the rate-limit markers, and a zero exit
  whose payload carries a `RATE_LIMITED` GraphQL error type.
- Serve-stale applies only to snapshots the current code understands
  (`schema_version` matches). An old-schema snapshot is treated like a missing
  one, because callers would otherwise read fields that no longer exist.
- The deferral floor is configuration (`[local] rate_limit_floor`, default 500,
  0 disables) rather than a constant, because acceptable headroom depends on
  how many repositories and agents share one account.
- Explicit `kanbanlan refresh` still fails loudly when rate limited. Only
  `ensure`, whose contract is "give me a usable snapshot", degrades to the
  cached document.
- The background worker keeps its existing failure backoff on rate limits;
  aligning its retry schedule with `resetAt` was left out to keep this change
  reviewable (see `docs/improvements/by-fable.md` for the wider plan).
- An adversarial review pass before delivery surfaced four issues that were
  fixed in place: the stale-service warning corrupted `--json ensure` stdout;
  `collect()` swallowed a peer repository's `RateLimitError` and wrote an
  "ok" snapshot missing that repository's pull requests; rate-limit detection
  scanned stdout, where quoted user content could false-positive; and a
  timezone-naive or non-string `resetAt` crashed the deferral guard.
- Known limitation, deliberately untouched: `snapshot.FileLock` waiters time
  out at 10 seconds and the 60-second mtime staleness rule can unlink a lock
  a slow refresh still holds, so the anti-stampede guarantee weakens for
  refreshes longer than those thresholds. Promoting `WorkerLock`'s
  owner-verified semantics into the cache lock is captured as its own
  request.

## Verification

- `uv run pytest`: 249 tests plus 28 subtests pass.
- `uv run ruff check .` and `uv run ruff format --check .` are clean.
- New tests cover: lock-wait reuse of a concurrent refresh, serve-stale plus
  `throttled` health on a rate-limited refresh, explicit refresh still
  raising, fatality when no usable snapshot exists, deferral below the floor
  and expiry of the deferral at reset, floor 0 disabling deferral, old-schema
  snapshots never being served stale, both `graphql()` classification paths,
  and `rate_limit_floor` config validation.
- Live validation during implementation: this account's GraphQL budget really
  was exhausted mid-session, and the new `RateLimitError` hint surfaced through
  the CLI error path exactly as designed.

## Delivered result

`ensure` now degrades in order of preference: fresh cache, a fetch someone
else just completed, a deferral that conserves the last points before reset,
and the last good snapshot when GitHub throttles the fetch. Health records
`throttled` distinctly from `error`, and `status`, `doctor`, and `--json
ensure` all report the remaining GraphQL points and reset time. Configurable
via `rate_limit_floor` in `.kanbanlan.toml`. Follow-up work captured
separately: worker cost halving (KBL-SAJYMLAS7FF5HAXWF3COXAOH2I) and
incremental item hydration (KBL-ZPRKDYD3AJCKLFJCVKVBU2XK4Q).
