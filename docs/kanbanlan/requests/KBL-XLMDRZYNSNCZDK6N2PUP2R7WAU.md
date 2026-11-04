# Remove the background worker and write-behind outbox

- Kanbanlan: `KBL-XLMDRZYNSNCZDK6N2PUP2R7WAU`
- Canonical home: `github`
- Canonical request: [#106](https://github.com/jmitchel3/kanbanlan/issues/106)

## Request

Lifecycle commands write GitHub synchronously as point writes; the snapshot refresh worker and outbox go away, with a migration that unloads an installed worker and drains or reports any queued outbox entries.

## Decisions

- Lifecycle commands (`capture`, `triage`, `claim`, `release`, `review`,
  `close`, `handoff`, `rehome`, and `start`/`done`/`drop` through them) are the
  former live commands, called directly. Each still reads and writes only its
  own card and keeps the existing transient-retry and rate-limit handling.
- After a write the shared snapshot is only invalidated; no detached refresh
  process is started. Reads refresh in the foreground when the snapshot is
  stale (the repository queue read is REST with ETags). `overlap` reads the
  Project live; the stale-while-revalidate path, `serveable`,
  `needs_revalidation`, and `refresh_for_write` are removed.
- `refresh.lock` (shared by sessions in different worktrees) and the
  owner-verified `FileLock` stay; the outbox, drain, worker, and registry locks
  are gone.
- The worker registry (`registry.py`) was worker-only state and is deleted;
  `state_dir()` moved to `config.py` for account bindings.
- Migration lives in `legacy.py`. `admin worker uninstall` (also `worker
  uninstall`) boots out `com.kanbanlan.worker`, disables
  `kanbanlan-worker.service`, deletes both unit files, SIGTERMs a worker still
  holding `worker.lock`, and removes the registry and lock files, keeping
  `accounts.json`. `doctor` warns on an installed unit, a running worker, or
  queued legacy changes.
- A legacy outbox is migrated by the first lifecycle command, under both of
  the old version's locks (`outbox.lock`, `drain.lock`) so an older session
  still running cannot enqueue or drain meanwhile. Queued and waiting changes
  replay in creation order as the live command; failed, interrupted, and
  unreadable ones, and later changes to a request whose change failed, are
  not run. Every change not applied is reported on stderr with the command
  that retries it; the outbox is then removed.

## Verification

- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`
  (478 passed), and `uv build` pass locally.
- New `tests/test_legacy.py` covers ordered replay, the failure and
  unreadable reports, the one-time migration, the replay child skip, the
  stderr report from a lifecycle command, unit removal with and without a
  service manager, and stopping a running worker.
- `tests/test_no_board_reads.py` asserts every lifecycle command reads only
  its card, for a fresh, old, and missing snapshot, with no refresh lock.
- Live latency on this repository (scratch card #113, closed not planned):
  `capture` 7.5s, `triage` 6.2s, `claim --no-worktree` 10.2s to 10.8s,
  `release` 7.1s, `start` on a Ready card 19.3s, `drop --close` 10.2s.
  A traced `claim` makes 8 GitHub calls of about 1s each plus the existing
  1s confirm pause. `start` adds the foreground queue refresh after the
  previous write (about 3.3s), the live Project read for `overlap` (about
  2.2s), the prune confirmation refresh and two `git fetch` runs.
- `kanbanlan doctor` on this machine warns that the old worker (started by
  0.12's `worker start`) is still running.

## Delivered result

Deleted `worker.py`, `outbox.py`, `registry.py`, the `worker` lifecycle and
`sync` commands, `docs/workflow/worker.md`, and their tests; lifecycle
commands write GitHub synchronously. This is a breaking change for the next
release: `worker` keeps only `uninstall`, `sync` is gone, and
`KANBANLAN_WRITE_BEHIND` and `KANBANLAN_BACKGROUND_REFRESH` no longer exist.

Follow-up: synchronous `start` is far from the 2s target. The largest costs
are the sequential claim calls (comment, verify read, confirm pause and read,
label read then edit, activity comment) and the full queue refresh that every
read pays after a write invalidates the snapshot.
