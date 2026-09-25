# Local-first lifecycle commands: instant local state with a write-behind GitHub sync

- Kanbanlan: `KBL-E4IBNUTYFJAM5JWGA4IZKYQU6Y`
- Canonical home: `github`
- Canonical request: [#67](https://github.com/jmitchel3/kanbanlan/issues/67)

## Request

## Outcome

Lifecycle commands return instantly. capture, triage, claim, release, review, close, and handoff validate against the local snapshot plus pending local changes, record the change in a per-repository outbox, and return; a detached sync process applies each change to GitHub in order under the bound gh account. Reads (ensure, next, overlap, status, record) serve the local view immediately and revalidate in the background.

## Acceptance criteria

- [x] Same-machine sessions arbitrate claims instantly under a local lock; the GitHub claim and its post-claim verification still run in the sync, and a lost cross-machine race is surfaced on the next command.
- [x] Pending changes overlay the snapshot for every local read and validation, and disappear only once the snapshot reflects them.
- [x] A failed change is never retried blindly; it is reported with the command that failed and blocks later changes to the same request.
- [x] kanbanlan sync shows, drains, and dismisses queued changes; the worker drains as a safety net.
- [x] Lifecycle commands complete in well under a second on a warm cache.
- [x] Tests cover overlay, ordering, failure propagation, and local claim arbitration.

## Decisions

- Replay, not a second implementation. An intent stores the argv of the
  ordinary live command, and the drainer runs it as a child process with
  `KANBANLAN_SYNC_EXECUTOR=1`. Every GitHub-side check and write (claim
  comment, post-claim verification, labels, Project Status, activity
  comments) is the code that already shipped, so the queued path cannot
  drift from the synchronous one. The foreground adds only local checks
  and an idempotent overlay effect per kind.
- Local arbitration: check-then-enqueue runs under `outbox.lock`, against
  the snapshot overlaid with every pending intent, so two sessions on one
  machine can never both queue a claim for the same card. Cross-machine
  safety is unchanged but asynchronous: the replayed claim posts and
  verifies, and a lost race fails the intent, which later commands report
  (with a note that the claim's worktree was created locally).
- Failure rules: a failed intent is never retried automatically; later
  intents for the same request fail as "not attempted"; an intent found
  `running` belonged to a dead drainer and is failed rather than replayed,
  because its effect on GitHub is unknown. `kanbanlan sync --retry` and
  `--dismiss` are the only ways forward, and a new command on that request
  refuses until then.
- A replayed `capture` carries its Kanbanlan ID (`--kanbanlan-id`, hidden)
  and first looks for an issue with that ID, so a retry after an unknown
  outcome never opens a second issue.
- Applied intents keep overlaying until one refresh after the batch lands,
  so a read never flickers back to the pre-change state; the drainer's
  children skip their own trailing refresh (`KANBANLAN_BACKGROUND_REFRESH=skip`)
  and the drainer refreshes once per batch.
- One drainer per repository (non-blocking `drain.lock`). After releasing
  the lock the drainer looks once more, closing the window where a session
  enqueues and its spawned drainer exits because the old one still held the
  lock. The worker drains each registered repository as a safety net.
- Local reads serve a snapshot up to `SERVE_STALE_FACTOR` (10) staleness
  windows old and spawn a background refresh when it is stale; older, or
  missing, waits for GitHub. Project scope has its own cache
  (`project_snapshot.json`) so the repository-scoped `snapshot.json`
  contract is untouched. `reconcile` answers locally only when the cached
  snapshot and cached open-issue list (`open_requests.json`, written by
  every board read) show no drift; any drift is re-confirmed live.
- Anything the local view cannot decide runs live after draining the queue:
  a request missing from the snapshot, a routed capture, `review` with no
  pull request in the snapshot yet (one opened moments ago), `claim
  --no-worktree`, `record` of a still-unnumbered request.
- A claim skips `git fetch` when `FETCH_HEAD` is under five minutes old; the
  branch base is then at most five minutes behind, and the fetch was the
  bulk of the remaining claim latency.
- `KANBANLAN_WRITE_BEHIND=0` restores fully synchronous behavior. The test
  suite sets it by default so existing command tests keep exercising the
  live path; `tests/test_write_behind.py` opts in.
- Sessions still on 0.10.0 or earlier do not read the outbox, so until every
  local session upgrades, same-machine arbitration covers only upgraded
  sessions; GitHub-side verification still covers everyone.

## Verification

- `uv run pytest`: 397 passed, 37 subtests; ruff check and format clean.
  `tests/test_write_behind.py` covers the overlay (pending capture,
  claim leaving the Ready queue, release, failed intents ignored,
  idempotence, no mutation of the snapshot), the drain rules (order, one
  refresh per batch, failure blocking only the same request, a stranded
  `running` intent failed, applied intents surviving a failed refresh, a
  held drain lock, a change queued mid-drain), and the commands (queued
  triage without GitHub calls, local rejection, two local claims
  arbitrated, refusal after a failed change, the live fallbacks, capture
  returning its ID and being immediately triageable, stale `ensure`
  revalidating in the background, `next` seeing queued changes, cached
  clean `reconcile`, drift re-confirmed live, `sync --retry/--dismiss`,
  executor error parsing, and idempotent capture replay).
- Live smoke test on this repository with a throwaway request (#70, closed
  as not planned): `capture` 0.18s, `triage` 0.20s, `claim` 1.00s
  (including a `git fetch` and worktree creation), `release` 0.26s,
  `close` 0.25s; warm `ensure` 0.17 to 0.23s, `next` 0.16s, `status`
  0.16 to 0.20s, `reconcile` 0.37s, `overlap` 0.23 to 0.26s. The drainer
  created #70, triaged it, claimed it (GitHub showed In progress under this
  session), then released and closed it as not planned; `gh issue view 70`
  reported `CLOSED NOT_PLANNED`.

## Delivered result

Lifecycle commands and board reads now answer from local state in about
0.2s: changes are validated locally, queued in a per-repository outbox,
and replayed to GitHub in order by a detached drainer under the bound gh
account, with the worker as a safety net. Same-machine claims are
arbitrated instantly; cross-machine conflicts are detected by the
replayed claim and reported on the next command. `kanbanlan sync` shows,
drains, retries, and dismisses queued changes.

This branch also documents the account binding from
KBL-LYSC3N56TRARVMHMQGQVBALUPE in the README.
