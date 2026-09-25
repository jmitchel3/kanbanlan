# Make lifecycle commands fast: concurrent reads, coalesced refreshes, fewer full-board refreshes

- Kanbanlan: `KBL-Q4CCZA4ZIFEQDHCOZOTZ5HASWE`
- Canonical home: `github`
- Canonical request: [#65](https://github.com/jmitchel3/kanbanlan/issues/65)

## Request

## Outcome

Kanbanlan lifecycle commands stop being the slow step in agent sessions. Today every live refresh issues its GitHub reads serially (project probe, hydration, per-repository pull requests), a claim performs three full-board refreshes, and sessions that queue on refresh.lock each repeat a full fetch after waiting, so parallel claims serialize at roughly 3 seconds per refresh per session.

## Acceptance criteria

- [x] Independent GitHub reads within one refresh run concurrently; GraphQL point cost is unchanged.
- [x] A session that queued on the refresh lock reuses a snapshot whose fetch started after it asked, instead of fetching again.
- [x] Mutating commands drop full-board refreshes that only warm the cache, without weakening claim race detection.
- [x] Measured before/after timings for ensure, reconcile, overlap, and claim recorded in the request record.
- [x] Tests cover the coalescing and concurrency paths.

## Decisions

- The cost was never CPU. Python import is 0.07s and a `gh` process adds
  about 0.05s over raw HTTPS, so rewriting in Rust or dropping `gh` would
  save little. Every second went to GitHub round trips (0.5 to 1.9s each)
  issued one after another, and to sessions queueing behind each other's
  full refreshes.
- `GitHub.collect` starts the pull request reads alongside the Project
  read. The repositories to read are only known once the Project arrives,
  so the previous read's list (now stored in the advisory item cache as
  `repositories`) is started speculatively; a repository no longer on the
  board is discarded (its result and any error ignored), and a newly
  referenced one is read afterwards. The snapshot is identical to the
  serial path. A speculative read for a departed repository is the only
  extra GraphQL spend, and it occurs once per departure.
- `CacheStore.refresh` coalesces: it records when it was called, and after
  acquiring the lock returns the existing snapshot when that snapshot's
  fetch started at or after that moment. Such a fetch reflects every write
  the caller made before calling, exactly as its own fetch would, so claim
  race detection (which relies on the post-CLAIM refresh) is unchanged. A
  fetch already in flight when the caller asked is never reused.
- Refreshes whose result nothing reads (the last step of capture, triage,
  claim, release, rehome, review, close, handoff) now invalidate the
  snapshot and refresh in a detached `kanbanlan refresh` process. The
  invalidation marker (`invalidated.json`) makes any snapshot generated
  before it count as stale, so an `ensure` before the background refresh
  lands reads the board itself instead of serving pre-mutation state; a
  failed background refresh costs one foreground read, never a wrong
  answer. `KANBANLAN_BACKGROUND_REFRESH=0` keeps it in the foreground, and
  the test suite sets it so fakes are never driven by a real process.
- `_set_state` writes the status label and the Project Status concurrently,
  and `read_board` (reconcile, capture, the worker, apply verification)
  lists open issues while the snapshot refreshes.
- Claim does not overlap worktree creation with the status move: if the
  status write failed after the worktree was created, the leftover worktree
  would block a retry with "worktree path already exists".
- Deferred to a separate request: a machine-local state store with a
  write-behind worker, so same-machine sessions arbitrate claims locally and
  commands return without waiting on GitHub at all.

## Verification

- `uv run pytest`: 352 passed, 37 subtests. `uv run ruff check .` and
  `uv run ruff format --check .` clean.
- `tests/test_fast_paths.py` covers coalescing (reuse only a fetch begun
  after the request; an in-flight fetch is not reused), invalidation,
  the detached background refresh and its foreground opt-out, speculative
  repository reads (departed repository discarded even when its read
  fails, new repository read after the Project), and concurrency of the
  Project and pull request reads, the two status writes, and `read_board`
  (barrier-based, so a serial implementation deadlocks and fails).
- Live timings against jmitchel3 Project 2 (two repositories), 0.10.0
  release versus this branch:

  | Command | Before | After |
  | --- | --- | --- |
  | `refresh` | 3.1s | 1.5s |
  | `overlap` | 3.1 to 3.7s | 1.5 to 1.7s |
  | `reconcile` | 3.9 to 4.0s | 1.9 to 2.4s |
  | 4 concurrent `reconcile` sessions | 13.8 to 16.7s | 1.9 to 2.5s |
  | `claim` (traced) | 18.9s | about 8s (estimated) |
  | `triage` (traced) | 12.0s | about 4.5s (estimated) |

  The claim and triage figures after the change are estimates built from
  the traced component timings (two 1.5s refreshes instead of three 3 to
  4.6s ones, and the label and Project writes overlapped); they were not
  re-run live because that needs another card to claim.

## Delivered result

Board reads run their independent GitHub calls concurrently, sessions
queued on the refresh lock reuse a fetch that began after they asked, and
mutating commands no longer wait for a cache-warming refresh. A single
refresh is about twice as fast, and concurrent sessions no longer pay for
one another's refreshes.

Follow-up: a local-first state store with a write-behind worker, which
would make lifecycle commands return without waiting on GitHub.
