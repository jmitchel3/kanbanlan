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
- `KANBANLAN_FULL_REFRESH=1` (also `true`/`yes`/`on`) forces the full fetch
  path, documented in the README rate-limit section.
- `_fetch_project` now reports the minimum `rateLimit` across probe,
  hydration, and full-fetch pages, matching the convention `collect()`
  already uses across queries.

## Verification

- `uv run pytest -q`: 268 passed (15 new behavior tests in
  `tests/test_incremental_hydration.py` covering unchanged-board reuse,
  content-only and item-only timestamp changes, added and removed items,
  corrupt, version-mismatched, and wrong-project caches, null hydration
  nodes, draft rehydration, 30-id batching, the forced-full environment
  variable, unwritable cache paths, and incremental output equality with the
  full fetch).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `uv build`: wheel and sdist built.
- Live smoke test against the real Project (31 items): warm unchanged
  refresh is one probe page at GraphQL cost 1 with zero hydration calls,
  versus one full page at cost 4; the incremental result compared equal to a
  forced full fetch of the same board, and two consecutive
  `uv run kanbanlan refresh` runs produced snapshots identical apart from
  `generated_at` and `rate_limit`.

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
