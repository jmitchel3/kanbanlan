# Lifecycle commands never block on GitHub, locks, or other sessions

- Kanbanlan: `KBL-YD4LHAICYZD7RJZVIFZOTMX4MQ`
- Canonical home: `github`
- Canonical request: [#92](https://github.com/jmitchel3/kanbanlan/issues/92)

## Request

Observed 2026-09-29/30 across rangertrac.org sessions: triage, claim (including claim --no-worktree), and reconcile repeatedly failed with 'timed out waiting for lock .../refresh.lock' or took minutes, because each live command reads the whole 1611-item Project several times under the single per-repository refresh lock, and _instant falls back to a live command (after draining every session's queue inline) whenever the cached snapshot is older than 10 x stale_seconds. Sessions reported the board as 'locked by other sessions' and stalled work.

Outcome: every lifecycle command (capture, triage, claim with or without a worktree, release, review, close, handoff) returns within a few seconds regardless of board size, other sessions, or GitHub availability. It decides from local state (a stale snapshot is acceptable; no snapshot at all may do one bounded read), queues the change, and exits. The drainer applies each change with targeted per-card reads and writes, never a full-board refresh and never refresh.lock, and verifies claim exclusivity against the live card at apply time, reporting a conflict instead of blocking up front. No command waits on refresh.lock; readers use the last snapshot. A regression test fails if a lifecycle command or intent executor reads the full board or acquires refresh.lock.

Out of scope: rate-limit retry and cooldown writes (#84, #83), worker dedupe (#86).

## Decisions

- A lifecycle command's live check is one card, not the board. The new
  provider call `read_request` (GitHub: `REQUEST_CARD_QUERY`, GraphQL cost 1)
  reads the issue with the same issue selection as the board read, its own
  Project item and Status for the configured Project (from
  `issue.projectItems`), and its closing pull requests. The result is passed
  through `build_snapshot` as a one-card snapshot, so claims, session history,
  status, and pull request linkage are normalized exactly as on a full read.
- With `pull_requests`, pull requests that only declare the request's
  Kanbanlan ID are found by one search restricted to the Project's
  repositories (from the same card read), split to fit GitHub's query length
  limit. Search indexing lags, so an identity-only pull request opened
  seconds ago may be missed; closing references are immediate.
- Every `_*_live` command (triage, claim, release, review, close, handoff),
  which is also what the drainer replays, now reads its card with
  `_read_card` and writes with `_set_state(provider, store, item, ...)`. A
  Kanbanlan ID resolves to an issue number from the cached snapshot, or one
  `find_request` search for a request newer than it. The Project Status
  write uses the cached Project fields and falls back to the fields-only
  `projection_metadata` read, the approach capture took in #89.
- Claim exclusivity is decided at apply time by the card's own claim
  comments: post CLAIM, re-read the card, and if the earliest unreleased
  claim is another session's, post RELEASED and fail the change with
  "claimed first by ...". A read that shows no claim yet is retried twice
  (0.5 s, 1 s) so read-after-write lag is not mistaken for a lost race; if
  none ever shows, the claim fails closed. A read showing this session as
  the winner is confirmed by one more read about 1 s later before the card
  moves to In progress, narrowing the window in which a lagging replica
  lets two sessions both win.
- Only a card on the configured Project counts. A live read of an issue
  with no (unarchived) item on the Project is refused with the board read's
  "not on the configured kanban home" error; nothing adds it to the Project
  or writes its Status. Only capture's placement and replay repair add an
  item. GitHub's answer that a card is not on the board is final at call
  time, never replaced by a stale snapshot.
- A card read live is the truth for that card. Applied changes whose
  `finished_at` is before the card read began (the card's `generated_at`
  is the read's start) are not laid over it again, the same rule `settle`
  applies to board snapshots; otherwise a card moved back since this
  machine's change would be judged by the old change.
- Reads made to decide at call time share one 15 s budget
  (`PLAN_READ_TIMEOUT_SECONDS`) across the Kanbanlan ID lookup, the card
  read, and any pull request searches, and never retry, since retry
  backoff alone could outlast it. `review` and `close` read the card with
  its pull requests the first time, so they need at most one card read.
  The drainer's replays keep the default timeouts and retries.
- `_instant` no longer falls back to a live command, and never drains other
  sessions' queues inline. A stale snapshot decides locally. The command
  reads its one card, within a 15 s budget, only when the request
  is missing from the local view, the snapshot is missing, or it is past its
  serving window; a failed read of a request the stale snapshot still has
  falls back to that snapshot, because the replay re-checks the live card.
  `review` without a known pull request re-reads the card with pull requests
  and refuses only if the live card has none. Only a capture routed with
  `--repository` (or given `--kanbanlan-id`) still runs live, without
  draining, since it creates a new request nothing queued can precede.
- `claim --no-worktree` now goes through the same write-behind path.
  Worktree creation for a normal claim is unchanged (created locally under
  the outbox arbitration lock).
- The drainer no longer refreshes the board after a batch. It spawns a
  detached `kanbanlan refresh` and applied intents keep overlaying until a
  snapshot whose read began after they finished lands; `settle` then drops
  them. `settle` runs in `refresh`, in local reads, in `_instant`, and at
  the start of each drain.
- Combined with #93 and #94 on rebase: `settle` removes only applied
  intents, so a quota-`waiting` intent is never settled or removed and the
  whole-queue hold still applies; `drain_outbox` keeps the detached refresh
  instead of #94's `refresh_for_write`, which remains only where a snapshot
  read genuinely happens (the `KANBANLAN_BACKGROUND_REFRESH=0` branch of
  `_refresh_after_mutation`). Every lifecycle replay, claim included, needs
  only its card, so all of them proceed during a board refresh cooldown.
- Readers (`ensure` and local views such as `next`) serve any usable
  snapshot and refresh in the background; only a missing snapshot makes
  them wait. `ensure` warns when the snapshot it served is past the serving
  window.
- `reconcile` is unchanged.

## Verification

- `uv run pytest -q`: 487 passed after rebasing onto #93, #94, and #95,
  including their tests and a new check that a replayed claim succeeds
  during a refresh cooldown. `uv run ruff check .` and
  `uv run ruff format --check .`: clean.
- `tests/test_no_board_reads.py` is the regression guard: a fake GitHub
  holding one card fails on `snapshot`, `fetch`, `collect`, or
  `list_open_requests`, and `CacheStore.refresh`/`ensure`, `read_board`, and
  entering any `refresh.lock` fail the test. It runs every lifecycle command
  (triage, claim, claim `--no-worktree`, release, release `--blocked`,
  review, close, handoff) both as the drainer replays it and as a session
  types it with a fresh, too-old, and missing snapshot, plus a replayed
  capture, a lost claim race, and `drain_outbox` itself. Against the
  previous code the same file fails 28 cases on "refreshed the shared
  snapshot". It also covers a queued capture (no GitHub calls), a session
  whose triage completes while another drainer holds `drain.lock` (and
  sessions never take `drain.lock`), an issue off the Project refused
  without any write on both paths, a claim that never becomes visible
  failing closed, and a claim whose confirming read reveals an earlier
  claim failing without moving the card.
- Review fixes are pinned by tests that fail without them: a live card is
  not overlaid with an applied change it already reflects (while a change
  applied after the read still is), the plan path passes a bounded,
  non-retrying timeout to the lookup and the card read, and `find_request`
  and `read_request` share one budget and stop once it is spent.
- New unit tests cover `read_request` (Project item selection by owner and
  number, claim and status normalization, identity search scoped to Project
  repositories, split search queries) and the local decision paths (card
  read for an unknown request, a too-old snapshot, review without a known
  pull request, GitHub unavailable with and without a snapshot).
- Live check against jmitchel3/kanbanlan with an isolated cache (a scratch
  clone, so no other session's queue was touched): `REQUEST_CARD_QUERY` cost
  1 point and took about 1.5 to 1.8 s with pull requests; it found #94 and
  #95 linked to #83 and #86 by their declared Kanbanlan IDs. On scratch card
  #96 (`KBL-R34UMMJMXRAKDN5VIC2N3K6BZA`), capture, triage, claim
  `--no-worktree`, release, a second claim, and close each returned in 0.2 to
  0.3 s; the drainer applied capture, triage, and claim within 15 s. A
  CLAIM comment posted directly by "other-machine" before the second claim
  synced made that change fail with "already has an active claim" instead of
  blocking. #96 was then closed as not planned and rests in Done.

## Delivered result

Lifecycle commands decide from local state and return in well under a
second, reading one card (bounded) only when the local view cannot decide.
Neither they nor the drainer's replays read the board or take
`refresh.lock`; each change is applied with per-card reads and writes, and
claim conflicts surface as failed changes in `kanbanlan sync`. Remaining
gaps: `reconcile` still reads the whole board when it finds drift or when
`--apply` is used; `record` for a request whose capture is still queued
still waits for the queue to drain (`_drain_inline`); back-to-back drains
can each start a detached refresh, which are not yet coalesced; `ensure`
serves a snapshot of any age while it refreshes, so a very old one is
answered with only a warning; identity-only pull requests newer than
GitHub's search index are not seen until the index catches up.
