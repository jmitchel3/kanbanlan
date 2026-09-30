# Re-queue outbox changes that failed on a rate limit once the quota resets

- Kanbanlan: `KBL-NII35LJJBRGWLAY5ILEOSSN7SY`
- Canonical home: `github`
- Canonical request: [#84](https://github.com/jmitchel3/kanbanlan/issues/84)

## Request

Observed 2026-09-28: a capture that failed with RateLimitError stayed failed after the reset passed; the waiting session polled for 25+ minutes and plain 'kanbanlan sync' never retried it. Only 'sync --retry ID' recovered it.

Outcome: an intent that failed with a rate-limit error records its reset_at and is automatically re-queued by the next drain (worker cycle or any sync) after that time, preserving order relative to later intents for the same request. Other failures stay terminal. 'sync' output shows 'waiting for quota reset at <time>' instead of failed.

## Decisions

- A quota refusal puts the intent in a new `waiting` state with `retry_at`
  instead of `failed`. `waiting` counts as pending, so `overlay()` keeps
  reflecting it in local reads, and it never blocks new changes the way a
  failure does.
- While any intent waits for a reset still ahead, the drainer runs nothing.
  Every queued intent shares the exhausted quota, so running them would only
  spend more refusals, and holding the whole queue keeps each request's
  changes in creation order without per-request bookkeeping.
- The first drain after the reset (worker cycle, post-command drainer,
  `sync --drain`, or a drainer that plain `kanbanlan sync` now starts when
  something is due) moves the intent back to `queued` with its original
  sequence, so it runs ahead of later intents.
- Detection trusts the child command's structured `--json` error: kind
  `RateLimitError`, now also reported for a `gh` command that failed on a
  rate limit, plus a `reset_at` field. The only message fallback is the
  child's own "to preserve quota" deferral wording; GitHub's rate-limit
  markers are never matched against messages, which can quote request
  titles. A missing or already-past reset waits 60 seconds.
- `sync --retry` and `sync --dismiss ID` also accept a waiting change.

## Verification

- `uv run pytest -q`: 442 passed. New tests cover waiting with the reset
  time, holding later intents, re-queueing in order after the reset, the
  60-second fallback, terminal non-quota failures, overlay of waiting
  intents, sync output and drainer start, structured error detection, and
  the child's `RateLimitError` kind and `reset_at` in `--json` errors.
- `uv run ruff check .` and `uv run ruff format --check .` pass.

## Delivered result

Outbox changes refused by GitHub for quota now wait for the reset and are
applied automatically by the next drain after it, in order; `kanbanlan sync`
shows them as "waiting for GitHub quota until <time>". Other failures stay
terminal. A live command that falls back from write-behind while a change
waits still runs ahead of it, as before.
