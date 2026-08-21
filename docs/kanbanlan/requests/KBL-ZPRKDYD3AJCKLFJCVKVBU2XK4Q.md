# Incremental item hydration for snapshot refresh

- Kanbanlan: `KBL-ZPRKDYD3AJCKLFJCVKVBU2XK4Q`
- Canonical home: `github`
- Canonical request: [#46](https://github.com/jmitchel3/kanbanlan/issues/46)

## Request

## Outcome

Refresh cost scales with what changed, not with board size.

- Fetch project items with a minimal field set plus updatedAt, diff against the cached snapshot, and re-fetch comments and full content only for changed items.
- Typical refreshes on a stable board drop to a small fraction of today's GraphQL point cost (comments(last:100) per issue is the dominant charge today).
- Larger change to PROJECT_QUERY and build_snapshot; needs its own review.

See docs/improvements/by-fable.md (request 3).

## Decisions

- Two-phase fetch inside `GitHub._fetch_project`: a cheap paginated probe
  (project metadata plus per item only `id`, `type`, `isArchived`,
  `updatedAt`, and content limited to Issue and PullRequest `id`, `number`,
  `updatedAt`, `repository`), then a `nodes(ids: [...])` hydration query,
  batched at 30 ids, that reuses the exact full item selection. The item
  selection is a shared constant (`PROJECT_ITEM_FIELDS`) interpolated into
  both `PROJECT_QUERY` and `ITEM_HYDRATION_QUERY`, so both paths produce
  identical raw nodes by construction and `build_snapshot` plus the snapshot
  schema are untouched.
- A cached raw node is reused only when the item id, the item-level
  `updatedAt`, and the content `updatedAt` all match the probe. Both
  timestamps matter because they move independently: a Status field edit
  bumps only the item's, a new comment or label bumps only the issue's.
  Probe nodes without both timestamps (draft issues, redacted items) always
  rehydrate, so doubt means rehydrate.
- The raw-node cache (`project_items.json` in the shared cache directory,
  written atomically via tempfile plus `os.replace` with 0o600, mirroring
  `CacheStore._write_json`) is purely advisory. Missing, corrupt,
  version-mismatched, differently keyed, or unwritable cache state falls back
  to the preserved full-fetch path and is rewritten from its result; any
  hydration surprise (null node, id or type mismatch) abandons the
  incremental attempt for that refresh. Cache state can therefore affect only
  cost, never the returned project. Project-scope reads share the path; the
  atomic write is what keeps their lock-free access harmless.
- The item-level `updatedAt` field was added to `PROJECT_QUERY` so the cache
  can be seeded from a full fetch; snapshot consumers ignore the extra key.
- The cache stores a SHA-256 fingerprint of the Project `fields` metadata,
  and a probe whose fresh fields differ invalidates every cached node. Field
  and option renames (the `ensure_status_options` Todo-to-Inbox setup flow,
  or a human editing the board) bump no item or content timestamp, yet the
  old names are denormalized into every cached `fieldValues` node; the
  fingerprint is the only signal that catches them.
- The probe additionally reads the bare issue `comments { totalCount }`
  (valid without pagination args, priced at zero nodes) and reuse requires
  it to match the cached node, because comment deletions, the claim ledger's
  failure mode, bump neither `updatedAt`. Comment edits have no cheap probe
  signal at all, so every cache entry carries a `fetched_at` and expires
  after `ITEM_CACHE_MAX_AGE_SECONDS` (6 hours); reused entries keep their
  original `fetched_at` so reuse can never extend the bound.
- `updatedAt` has one-second resolution, so an entry whose content changed
  within `ITEM_CACHE_TIMESTAMP_SLACK_SECONDS` (2 seconds) of its own fetch
  is never reused; without this, a second change landing in the fetch second
  would compare equal forever.
- Reuse also requires the probe's `type`, `isArchived`, content `id`, and
  content `repository` to match the cached node, closing the transfer and
  archive-flip cases that timestamps do not reliably signal.
- An item deleted between probe and hydration surfaces as a GraphQL errors
  array (alongside a null node), which `graphql()` raises as RuntimeError;
  `_hydrate_items` treats any RuntimeError from a batch as a fallback to the
  full fetch, while `RateLimitError` propagates so the serve-stale path in
  `ensure` still works.
- `KANBANLAN_FULL_REFRESH=1` (also `true`/`yes`/`on`) forces the full fetch
  path, documented in the README rate-limit section.
- `_fetch_project` now reports the minimum `rateLimit` across probe,
  hydration, and full-fetch pages, matching the convention `collect()`
  already uses across queries.

## Verification

- `uv run pytest -q`: 276 passed (23 behavior tests in
  `tests/test_incremental_hydration.py` covering unchanged-board reuse,
  content-only and item-only timestamp changes, comment deletion via
  totalCount, Status option renames via the fields fingerprint, cache-entry
  expiry, the one-second-resolution slack window, preserved `fetched_at` on
  reuse, added and removed items, corrupt, version-mismatched, and
  wrong-project caches, hydration of an item deleted between probe and
  hydration, rate-limit propagation from hydration, multi-page probes, draft
  rehydration, 30-id batching, the forced-full environment variable,
  unwritable cache paths, a probe-query shape guard pinning the test mirror
  to `PROBE_ITEM_FIELDS`, and incremental output equality with the full
  fetch).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `uv build`: wheel and sdist built.
- Live smoke test against the real Project (31 items): warm unchanged
  refresh is one probe page at GraphQL cost 1 with zero hydration calls,
  versus one full page at cost 4; the incremental result compared equal to a
  forced full fetch of the same board, and two consecutive
  `uv run kanbanlan refresh` runs produced snapshots identical apart from
  `generated_at` and `rate_limit`. A stale schema-v1 cache correctly fell
  back to the full fetch and was rewritten as v2.
- Live verification of the comment timestamp assumptions, on a scratch issue
  (jmitchel3/kanbanlan#52, plain issue, never added to the Project): adding
  a comment bumped the issue's `updatedAt`; editing the comment body did not
  change `updatedAt` at all (confirming that only the cache expiry bounds
  how long an edited body can be served); deleting the comment did bump
  `updatedAt` in this observation, but that behavior is undocumented, so the
  probe's `comments { totalCount }` check remains the guarantee for
  deletions. The bare `comments { totalCount }` selection is valid GraphQL
  without pagination arguments at query cost 1. The scratch issue could not
  be deleted (`gh issue delete` returned "Viewer not authorized to delete"),
  so it was closed as not planned instead; it carries no labels and no
  Project item, so the board is untouched.

## Delivered result

`GitHub._fetch_project` now probes item identity and timestamps, reuses
unchanged raw nodes from an advisory on-disk cache, and hydrates only new or
changed items in batches, so refresh cost scales with what changed instead of
with board size. A stable board pays roughly a quarter of the previous point
cost per refresh page (probe cost 1 versus full cost 4 measured live), and
the dominant `comments(last: 100)` charge is now paid only for items that
actually changed. The full-fetch path is preserved as the fallback for every
doubtful case and can be forced with `KANBANLAN_FULL_REFRESH=1`. Follow-up
worth considering: prune or cap the cache for very large boards, and extend
the probe to draft issues if boards ever carry many drafts.
