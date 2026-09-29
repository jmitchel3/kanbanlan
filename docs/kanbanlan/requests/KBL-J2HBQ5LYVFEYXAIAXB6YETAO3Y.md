# Capture must not create an issue and then time out on refresh.lock during Project setup

- Kanbanlan: `KBL-J2HBQ5LYVFEYXAIAXB6YETAO3Y`
- Canonical home: `github`
- Canonical request: [#85](https://github.com/jmitchel3/kanbanlan/issues/85)

## Request

Observed 2026-09-28 in rangertrac.org (1611-item Project): 8 captures (#3238, #3242, #3247, #3258, #3261, #3290, #3296, #3300) created the GitHub issue, then failed with 'Project setup failed: timed out waiting for lock .../refresh.lock', leaving half-created requests that need reconcile --apply. Claims failed on the same lock timeout. A full refresh of a large Project holds the lock for minutes, and each queued change waited on it serially (6 to 17 minutes per capture).

Outcome: capture adds the issue to the Project and sets fields with targeted mutations that do not wait on refresh.lock; the executor never holds the drain queue behind a full-board refresh. A lock timeout after issue creation is retried or repaired automatically instead of surfacing as a failure.

## Decisions

- Capture no longer reads the board. The old path refreshed the whole
  Project (under `refresh.lock`) up to five times per capture to find the new
  item, then ran repository-wide reconciliation; on a 1611-item Project that
  held the drain queue for minutes per change and timed out on the lock
  after the issue already existed.
- `_place_captured_request` takes the Project item id from
  `gh project item-add --format json`, and writes Status from the cached
  snapshot's `project` fields. If the cache is missing, or the write fails
  because cached field or option ids went stale, it retries once with
  `projection_metadata()`, a new query that reads the Project's fields but no
  items, so its cost is fixed regardless of board size.
- The explicit `set_request_status(..., "status:intake")` call is gone:
  `create_issue` already applies that label.
- Capture no longer runs repository-wide reconciliation as a side effect;
  drift on other cards is `reconcile`'s job.
- Replay lookup (`_existing_request`) answers from the cached snapshot
  (ignoring pending overlay items, which have no number), then
  `find_request`: the newest 30 issues first, then an `in:body` search.
  Search alone was measured to miss an issue created seconds earlier and
  produced a duplicate, so the direct recent read is required.
- A replay that finds its issue still open with `status:intake` and absent
  from the cached board re-runs placement (item-add is idempotent). An issue
  that has progressed past intake is never touched, so a stale cache cannot
  regress a card to Inbox.
- The snapshot is invalidated and refreshed once after every local capture
  (in the background, or by the drainer under `KANBANLAN_BACKGROUND_REFRESH=skip`),
  not only when session tracking is on, since capture no longer refreshes it
  itself.

## Verification

- `uv run pytest -q`: 427 passed. New tests cover: capture places the card
  without any board read (`snapshot`, `list_open_requests`, `store.refresh`
  never called) for local and routed targets; missing cache reads only
  Project fields; stale cached field ids fall back to fresh fields; a missing
  item id is a Project setup failure; `find_request` finds a just-created
  issue without search, finds older ones by search, and ignores bodies that
  merely mention the id; replay repairs an intake issue left off the board and
  never touches one that progressed.
- `uv run ruff check` and `ruff format --check`: clean.
- Live, against jmitchel3/kanbanlan: `projection_metadata()` returned the
  Project id and Status field; `find_request` found #85 and returned None for
  an unused id. An end-to-end executor capture created #87 and placed it in
  Inbox in 6.7 s (previously minutes on large boards). The first replay,
  search-only, created duplicate #88; after adding the recent-issues read,
  the replay returned the existing issue in 1.0 s with no new issue. #87 and
  #88 were closed as not planned and #88 given a fresh id so reconcile is
  clean.

## Delivered result

`kanbanlan capture` writes only the new card: create issue, add to Project,
set Status, with no full-board read and no `refresh.lock` wait, so a queued
capture no longer blocks the drain behind a large Project refresh or leaves a
half-created request. A retried capture finishes placement instead of
needing `reconcile --apply`.

Follow-ups: #83 (writes during refresh cooldown), #84 (re-queue rate-limited
changes), #86 (worker refresh dedupe). Other lifecycle commands (triage,
claim) still take a full refresh and can hit the same lock timeout.
