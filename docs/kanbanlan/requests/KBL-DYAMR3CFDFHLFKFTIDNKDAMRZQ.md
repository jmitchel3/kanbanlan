# Read the request queue over REST with ETag conditional requests

- Kanbanlan: `KBL-DYAMR3CFDFHLFKFTIDNKDAMRZQ`
- Canonical home: `github`
- Canonical request: [#102](https://github.com/jmitchel3/kanbanlan/issues/102)

## Request

Repository-scope queue reads use REST with If-None-Match so unchanged reads return 304 and cost no rate limit. GraphQL stays only for Project field writes and project-scope reads. Lifecycle commands read only their own issue.

## Decisions

- Status labels are canonical, so labels plus issue, comment, and pull
  request data answer every repository-scope queue question. `GitHub.queue_snapshot`
  reads `issues?state=all`, the repository-wide `issues/comments`, and
  `pulls?state=open` for every repository the Project references, all over
  REST with `gh api --include` and `If-None-Match`. Per-page ETags, headers
  (Link included), and bodies are kept in `rest_cache.json` in the private
  cache dir. Pages the previous read followed are revalidated concurrently,
  then the Link chain is checked page by page, so a page that moved never
  contributes. Every read still asks GitHub; the cache only avoids paying for
  unchanged pages.
- The snapshot JSON keeps schema_version 3 and every key. New:
  `source.read_from` (`"issues"` or `"project"`; absent means project). In an
  issues read, Status comes from the single status label (`Done` once
  closed), `project_item_id` from the last Project read's item cache,
  `rate_limit` is the REST core quota with `resource: "core"` and `cost: 0`,
  closing references come from GitHub's closing keywords in the PR body (code
  and HTML comments ignored), and `merge_state` is null because REST lists
  do not carry it.
- An issue counts as a request when it carries a Kanbanlan ID, an open
  status label, or was on the board at the last Project read.
- Project fields and linked repositories are cached in
  `project_metadata.json`, rewritten by every Project read and otherwise
  renewed by one metadata-only GraphQL read at most every six hours. If that
  renewal is refused, the older copy (or the bare configured Project) is used.
- `CacheStore.refresh(client, projection=True)` selects the Project read.
  Reconcile (`read_board`, `apply_reconciliation`), `init`, and the worker
  use it; `ensure`, `refresh`, the background refresh, `cleanup`, and
  post-write refreshes use the queue read. The reconcile local fast path only
  trusts a snapshot whose `read_from` is `project`. Queue readers are detected
  on the class (`reads_queue`), so mocks and fakes keep the Project path.
- The GraphQL `rate_limit_floor` no longer holds back queue reads, and a REST
  `rate_limit` never trips it. A recorded GitHub cooldown still applies.
- Lifecycle commands keep their 0.12 point reads (`read_request`), which need
  the issue's Project item and Status and so stay one small GraphQL query.

## Verification

- `tests/test_rest_queue.py`: a fake `gh` answering ETags and 304s covers
  label-derived Status, claim comments, closing-keyword links, 304
  revalidation with zero GraphQL calls, a changed page, a new page past the
  cached chain, item ids from the Project cache, peer repositories and their
  failure, REST quota refusal, metadata renewal under refusal, the read
  routing in `CacheStore`, and snapshot key parity with the Project read.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`
  (507 passed), `uv build`.
- Live, in an isolated clone of this repository with a `gh` shim summing each
  response's reported `rateLimit.cost`, `ensure` then `next` with
  `KANBANLAN_WRITE_BEHIND=0`:
  - before (0.12.0): cold cache 8 GraphQL points (2 + 4 + 2), stale snapshot
    with warm item cache 5 points (probe 2, pull requests 2, hydration 1);
    `next` on a fresh snapshot 0.
  - after: cold cache 1 point (the one metadata read), stale snapshot 0
    points with all 6 REST pages answered 304; `next` 0.
  - `rateLimit.used` deltas were also taken but are dominated by concurrent
    agents on the same account and a window reset, so the per-response costs
    are the reliable figure.
  - The REST snapshot matched the Project snapshot item for item (status,
    claims, linked PRs, item ids once the item cache existed), apart from
    `merge_state` and live changes made between the two reads.

## Delivered result

Repository-scope reads spend no GraphQL points once warm: the queue is read
over REST with persisted ETags, and unchanged pages come back 304. GraphQL
remains for reconcile drift detection, project-scope reads, lifecycle point
reads, and Project Status writes. `status` and `doctor` name the quota they
report (REST requests or GraphQL points). Scaling note: the comments read is
repository-wide, so a repository with thousands of comments revalidates one
request per 100 comments (free when 304, but each is a round trip).
