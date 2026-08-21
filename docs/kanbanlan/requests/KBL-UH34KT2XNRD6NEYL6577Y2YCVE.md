# Cache refresh lock uses owner-verified semantics

- Kanbanlan: `KBL-UH34KT2XNRD6NEYL6577Y2YCVE`
- Canonical home: `github`
- Canonical request: [#48](https://github.com/jmitchel3/kanbanlan/issues/48)

## Request

## Outcome

The shared snapshot cache lock stops undermining the anti-stampede guarantee for slow refreshes.

Today snapshot.FileLock waiters time out after 10 seconds and a 60 second mtime staleness rule can unlink a lock a live, slow refresh still holds; the holder's exit then unconditionally unlinks whichever lock file replaced it. A project-scope refresh (paginated project read plus per-repository PR queries with retries) can exceed both thresholds, at which point sessions stop queuing and refresh concurrently again.

Promote worker.WorkerLock's semantics (PID liveness plus st_dev/st_ino identity, removal only when the recorded owner is provably gone) into a single shared lock used by the cache, and make lock release remove only a lock the holder still owns. Earlier reviews suggested the same for RegistryStore (docs/improvements/by-claude.md finding 9).

Surfaced by the adversarial review of KBL-2FFYGZHCYNEPXPEU5D6TIHV7GM.

## Decisions

- One shared implementation lives in a new `kanbanlan.locks` module: the
  blocking `FileLock` plus the primitives `file_identity`,
  `unlink_if_unchanged`, `lock_pid`, `pid_running`, and `write_owner_record`.
  `WorkerLock` keeps its own non-blocking acquisition loop (raise
  `WorkerAlreadyRunning` and report the existing owner) but now builds on the
  same primitives, so there is no third copy of the safety-critical logic.
- Staleness is now defined by owner liveness, never by age: a waiter removes
  an existing lock only when its recorded PID is provably dead
  (`os.kill(pid, 0)` semantics), and removal goes through
  `unlink_if_unchanged`, which verifies `(st_dev, st_ino)` identity so a
  concurrent replacement is never deleted. The 60 second mtime rule survives
  only as a last resort for a lock file with no readable PID, because such a
  file carries no liveness signal at all.
- Release verifies ownership too: `__exit__` unlinks only when the file on
  disk is still the exact file this process created. The old lock closed a
  descriptor and unconditionally unlinked whatever was at the path.
- The lock payload is the same JSON owner record the worker already writes.
  `lock_pid` additionally accepts a bare integer so a live legacy holder
  (written by the previous cache lock as a bare PID) is still honored across
  an upgrade instead of being misread as unreadable.
- The waiter timeout default rises from 10 to 30 seconds. A project-scope
  refresh paginates the whole Project and then runs per-repository pull
  request queries with retries, which legitimately exceeds 10 seconds; at 30
  seconds waiters stay queued through a realistic slow refresh while an
  unreadable-lock hang still fails loudly and bounded. A live owner is never
  preempted regardless of the timeout; expiry only makes the waiter give up.
- Coordination with the concurrent GraphQL fetch rework: the
  `FileLock(path, timeout)` constructor-and-context-manager interface and the
  `FileLock` name in `kanbanlan.snapshot`'s namespace are preserved (now an
  import), and `CacheStore.refresh`/`ensure` bodies are untouched, so that
  branch merges cleanly. `RegistryStore` imports the lock from
  `kanbanlan.locks` directly, dropping its dependency on `snapshot`.

## Verification

- `uv run pytest -q`: 261 tests plus 37 subtests pass, including new
  behavior tests: a live owner's lock is not stolen even with an hour-old
  mtime (JSON and legacy bare-PID records), a dead owner's lock is atomically
  replaced, release leaves a lock the holder no longer owns (file swapped
  underneath), the unreadable-lock mtime fallback frees only old files, and
  the registry lock both refuses to steal from a live owner and replaces a
  provably dead one.
- Existing `WorkerLock` tests pass unchanged, confirming its external
  behavior (non-blocking acquire that reports the existing owner) is intact,
  including the tests that patch `kanbanlan.worker._pid_running`.
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `uv build`: sdist and wheel build successfully.

## Delivered result

The snapshot cache refresh lock and the user-global registry lock now use
owner-verified semantics: acquisition records the owner PID, waiters steal
only from a provably dead owner, and every removal (steal or release) checks
file identity first, so a live, slow refresh can no longer have its lock
unlinked out from under it and sessions keep queuing instead of refreshing
concurrently. The logic is shared between the cache, the registry, and the
worker through `src/kanbanlan/locks.py`; no public CLI behavior changed. No
follow-up work remains from this card.
