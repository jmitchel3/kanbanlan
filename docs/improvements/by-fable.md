# GraphQL snapshot refresh: rate-limit analysis and remediation plan

Reviewed at `4fc3197` (0.8.0). Focus: the recurring GraphQL rate-limit failures
during snapshot refresh across projects, plus improvements in the same family
(refresh cost, refresh coordination, degraded-mode behavior). This narrows and
updates the broader 0.4.0 reviews in `by-claude.md` and `by-grok.md`; both of
those already flagged this cluster (Claude findings 5, 6, 7; Grok finding 11)
and none of it has landed as of 0.8.0.

Observed while writing this: `rate_limit.remaining` on the live snapshot was
1305 of 5000, on a quiet afternoon, with only one repository registered on this
machine. The budget is a per-user hourly pool shared by every repository, every
worker, and every agent session using the same GitHub account, which is why the
problem shows up "on a bunch of different projects" at once: they are all
draining one bucket.

## Why refreshes are so expensive

Every refresh, in every trigger path, does all of this:

1. `_fetch_project` paginates the whole Project, and each item page carries
   `comments(last: 100)`, `labels(first: 50)`, `assignees(first: 20)`, and
   `fieldValues(first: 30)` for every issue (`github.py:17-112`). GraphQL cost
   is computed from requested node counts, so each page is priced as if every
   issue really had 100 comments. This is the dominant per-refresh cost, and it
   is paid even when nothing on the board changed.
2. `collect()` then runs a paginated open-PR query per repository the Project
   references (`github.py:616-649`). A shared Project with N repositories
   multiplies every refresh by N.
3. The background worker pays it all twice per cycle: `refresh` before
   reconciling and a second full `refresh` plus a second `list_open_requests`
   as verification, even when there was no drift at all
   (`worker.py:228-258`).

## Why refreshes happen more often than they should

4. **The stampede.** `CacheStore.ensure` checks freshness before taking the
   lock; `refresh` then re-fetches unconditionally (`snapshot.py:522-559`).
   Several agent sessions starting around the same time all see "stale", all
   queue on the `FileLock`, and each performs its own full fetch back to back.
   The first fetch already made the cache fresh for the rest. With
   `stale_seconds = 180` this recurs every three minutes under multi-agent use.
5. **No budget awareness.** `fetch` carefully records the minimum `rateLimit`
   across queries into every snapshot, and nothing ever reads it back. There is
   no deferral when `remaining` is low, no warning, and no visibility in
   `status` or `doctor`.
6. **Rate-limit errors are opaque and unhandled.** A `RATE_LIMITED` GraphQL
   error or a secondary-rate-limit rejection surfaces as a generic
   `RuntimeError` from `graphql()` (`github.py:336-359`). `Runner.run` retries
   only 5xx-style transient markers, so a rate-limited read is retried never;
   worse, a rate-limited `refresh` hard-fails `ensure` even though a perfectly
   usable last-good snapshot is sitting on disk. The worker then counts it as a
   failure and backs off, but foreground agents just see refresh errors.

## Remediation plan

Ordered as independently reviewable outcomes; the first two are being
implemented now, the third is captured for later.

### Request 1: refresh degrades gracefully under rate-limit pressure

- Re-check freshness inside the refresh lock so waiting processes reuse the
  fetch that just completed instead of repeating it (kills the stampede).
- Classify rate-limit failures: raise a typed `RateLimitError` (carrying
  `resetAt` when GitHub reports it) from `graphql()` for `RATE_LIMITED`
  GraphQL errors and secondary-rate-limit rejections.
- Serve stale on throttle: when `refresh` fails with `RateLimitError` and a
  last-good snapshot exists, `ensure` returns that snapshot, records
  `refresh_status: "throttled"` in health, and warns instead of failing. Only
  a missing snapshot makes rate limiting fatal.
- Proactive deferral: when the cached snapshot's own `rate_limit.remaining` is
  below a floor and `resetAt` has not passed, `ensure` keeps serving the
  cached snapshot rather than spending points that are about to run out.
- Surface `rate_limit.remaining`, `resetAt`, and the throttled state in
  `status` and `doctor` so the condition is diagnosable instead of mysterious.

### Request 2: the worker stops paying double for a clean cycle

- When `plan_reconciliation` reports no drift, skip the verification refresh
  and the second `list_open_requests`. A clean cycle becomes one project read
  plus one PR sweep instead of two of each, roughly halving steady-state
  worker spend. Verification still runs after any applied repair.

### Request 3 (captured, not yet implemented): incremental item hydration

The structural fix for cost item 1: fetch project items with a minimal
field set plus `updatedAt`, diff against the cached snapshot, and re-fetch
comments and full content only for items that changed. Typical refreshes on a
stable board would drop to a small fraction of today's point cost. This is a
larger change to `PROJECT_QUERY` and `build_snapshot` and deserves its own
request and review.

## Related gaps checked and found already fixed in 0.8.0

- Subprocess timeouts and transient-5xx retry now exist in `Runner`
  (`runner.py:13-86`).
- CI has `timeout-minutes` at the job and step level.
- Comment-window truncation is now at least visible
  (`session_history_truncated`), though `active_claim` still only sees the
  last 100 comments; the durable-claim-marker idea from the earlier reviews
  remains worth doing eventually.

## Still open from the earlier reviews, adjacent but out of scope here

- Version string duplicated between `pyproject.toml` and `__init__.py`.
- No `py.typed`, no type checker in CI, no coverage floor, no changelog.
- Worktrees created by `claim` are never cleaned up.
- `cli.py` size and dispatch-through-`globals()`.
