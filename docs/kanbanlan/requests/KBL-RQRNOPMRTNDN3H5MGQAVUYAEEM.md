# Make start finish in under two seconds

- Kanbanlan: `KBL-RQRNOPMRTNDN3H5MGQAVUYAEEM`
- Canonical home: `github`
- Canonical request: [#116](https://github.com/jmitchel3/kanbanlan/issues/116)
- Pull request: [#120](https://github.com/jmitchel3/kanbanlan/pull/120)

## Request

start takes about 19s: a claim makes about 8 sequential GitHub calls plus a 1s sleep, and every write marks the snapshot stale so the next read blocks on a full refresh (about 3.3s). Run independent writes concurrently, drop the sleep, and update the cached snapshot in place for the card a write touched instead of invalidating it. Target: start under 2s on a warm cache, measured.

## Decisions

- Profiled first. `start` on a Ready card with a fresh cache, against main at 42b98c1, ran 13.5 to 14.6 s. Every step ran one after another (seconds; from the `old5` run):

  | Step | Before | After |
  | --- | --- | --- |
  | Project overlap read (3 GraphQL pages) | 2.1 | runs beside the claim |
  | Live read of this repository's claims (#119, REST) | 1.9 | runs beside the claim |
  | Claim pre-read (GraphQL) | 0.7 | skipped; the local view already checked the card |
  | Claim comment (`gh issue comment`) | 1.3 | one `addComment` mutation that returns the card: 1.4 to 1.7 |
  | First verify read | 0.8 | the mutation's response |
  | Confirm delay | 1.0 | 1.0 (kept) |
  | Confirm read | 0.9 | 0.9 to 1.2 (kept) |
  | Status writes (`gh issue view` + `gh issue edit`, beside `gh project item-edit`) | 2.3 | REST label add and remove, beside one `updateProjectV2ItemFieldValue` mutation: 1.0 to 1.2 |
  | `git fetch` + `git worktree add` | 0.5 | runs beside the claim |
  | Activity comment (`gh issue comment`) | 1.5 | one REST call, beside the status writes |
  | Repeated `git rev-parse` and `gh auth token` per step | about 0.5 | cached once per process |
  | Snapshot invalidated, so the next read refreshes (about 2 to 3) | yes | the card is written into the snapshot in place |

- The claim's race-safety protocol is kept as it was. The earliest unreleased CLAIM comment wins. A claim that does not show yet is read again. After a winning read, the command waits 1 s and reads again before the card moves. A posted claim is only kept once every check passes: the Project and live-claims reads (the "session already owns a card" checks), a still-Ready, still-open card when the claim skipped its pre-read, and a built worktree. Otherwise the claim is released with a RELEASED comment and the worktree and branch it built are removed, all before any status write. So a refusal that `start` used to make before posting is now made after posting and then released. The end state is the same.
- An Inbox card or a new title still waits for the Project and live-claims checks before anything writes, so nothing is triaged or captured for a session that already owns a card.
- `CacheStore.update_card` writes only the card's own fields (status, labels, claim, state, and so on) into `snapshot.json`, and recomputes `status_counts` and `next_ready`. Board-level fields (linked pull requests, Project item id) are kept. It tries `refresh.lock` once with `timeout=0` and never waits. If a refresh holds the lock, that refresh may have read GitHub before this write, so the snapshot is marked stale, as before. Triage, claim, release and review update in place. Capture, close, handoff and rehome still invalidate. `guard_refresh_lock` in the tests now refuses only a waiting acquisition.
- Under 2 s is not reachable without dropping a guarantee. After the local work, the critical path is three GitHub round trips plus the 1 s confirm delay: claim mutation, wait, confirm read, status write. Each `gh` call measured 0.6 to 1.7 s here, so the floor is about 3.5 to 5 s. Getting lower would mean moving the card before the confirm read (which `test_a_claim_is_confirmed_by_a_second_read_before_it_moves_the_card` forbids) or dropping the confirm read entirely.
- Interaction with #119 (merged while this was in flight): its live claims read now runs beside the claim instead of before it. Its two start tests now expect the claim step to run and be refused at its gate, and a new test covers the Inbox path being refused before triage.

## Verification

- [x] `uv run pytest` (508 passed)
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv build`
- New tests: in-place snapshot write after triage and claim; stale marking while a refresh holds the lock, or when the card is missing; a handed-over card claimed without a pre-read; a handed-over card that moved, released with its worktree and branch removed; a gate refusal and a worktree failure, each released before any status write; the comment-and-read mutation path reading only to confirm; REST comment and label calls (including a tolerated 404); the single Project status mutation; start passing the card and gate to its claim.
- Live end to end, timing `kanbanlan start '#118'` from Ready with a fresh cache. #118 was a throwaway card made for this. Between runs it was released, and its worktree and branch were removed. When done it was closed as not planned, and its worktree and branch were deleted.

  | Code | Run 1 | Run 2 | Run 3 |
  | --- | --- | --- | --- |
  | main 42b98c1 | 14.6 s | 13.5 s | 13.7 s |
  | this branch | 4.4 s | 5.2 s | 4.2 s |

  Before #119 merged, the same comparison was 11.9 to 14.4 s against 4.8 to 5.4 s.

## Delivered result

`start` on a Ready card now takes about 4 to 5 s instead of about 14 s, keeps the claim's race-safety protocol and the one-card-per-session check, and leaves the shared snapshot fresh with the claimed card in it, so the next command does no board read. The 2 s target is bounded by the confirm delay and three sequential GitHub round trips (see Decisions).
