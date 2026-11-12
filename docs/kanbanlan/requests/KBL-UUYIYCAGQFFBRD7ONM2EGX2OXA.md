# Snapshot refresh drops cards that are on the Project

- Kanbanlan: `KBL-UUYIYCAGQFFBRD7ONM2EGX2OXA`
- Canonical home: `github`
- Canonical request: [#117](https://github.com/jmitchel3/kanbanlan/issues/117)
- Pull request: [#119](https://github.com/jmitchel3/kanbanlan/pull/119)

## Request

#103 was on Project 2 and not archived but never appeared in snapshot.json, even after a forced full refresh. As a result, record #103 failed, and start's one-card-per-session check missed the claim and created junk card #109. Find the root cause, fix it, and add a regression test.

## Decisions

- Root cause of #103: GitHub's Project `items` connection left out issue #103's item while the issue's own `projectItems` listed it on Project 2, not archived. The #102 session's cold-cache GraphQL Project read at 15:46Z on 2026-11-04, from an isolated clone, already showed "only rest: [103]" against the REST issue list. The forced full refresh at 15:50Z (installed 0.12.0, `KANBANLAN_FULL_REFRESH=1`) also missed it. `gh api graphql ... issue(number:103){projectItems}` showed `PVTI_lAHOABXRhs4BeiXUzhGBMAE`, not archived, created 15:34:06Z, in order with #102 and #104 to #107. Kanbanlan did not filter the card out. The items connection never returned it, so every reader that trusts that connection to be complete dropped it: the repository snapshot, the project-scope copy that `overlap` reads, and `record`. The incident is not reproducible now (a live read lists all 67 items). The comparison from that session shows #103 was the only card missing.
- A Project read now cross-checks the configured repository's open requests (an open issue with a Kanbanlan ID or an open status label) against the items it returned. Each request it left out is read from the issue side with one batched `nodes(ids:)` query over `projectItems`, using the same item selection as the board read. It joins the read only if it is on the configured Project and not archived. The issue list is the queue read's conditional REST list (`issues_path`), so when nothing changed it costs a few 304s and no GraphQL points. A complete read looks nothing up. If the list cannot be read, the Project read is left as it was. Only the configured repository is cross-checked, because listing every peer repository's issues would add a REST chain per peer on every read.
- A second, latent cause of dropped cards was in the REST reader from #110. A `304` carries no Link header, and GitHub's ETag covers the body only. Both were measured against api.github.com: the same body under three different Link headers had the same ETag. So a full last page that gains a successor still answers `304`, and its cached Link ends the chain before the new page. Every issue (or claim comment) created there would be dropped until an earlier issue changed. This repository would hit it at 200 issues and pull requests, and at every 100 comments. A `304` for a full page with no cached next link is now read again without the ETag. That costs one counted request only while a collection's last page is exactly full.
- `start` now reads this repository's claims live (`store.refresh`, the conditional REST queue read, begun after the call) before it claims or captures anything. It adds that result to the local view and the Project overlap it already checked. The live read takes membership from the issue list, not the Project items connection, so an incomplete board read cannot hide the session's claim. A live read that fails refuses the start (fails closed).
- `record <ref>` falls back to a live read of the one card when the local view does not have it, as `done` and `start` already did. Before, it failed with "not on the configured kanban home".
- Tests stub `GitHub._repository_issues` by default (in `tests/conftest.py`), so a Project read in a test never runs a real `gh` issue list. The cross-check tests drive it directly.

## Verification

- [x] `uv run pytest` (489 passed, 98 subtests)
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv build`
- `tests/test_omitted_cards.py` adds 10 tests. Each fix was reverted on its own to confirm its regression test fails without it: the full-last-page reader test (rest.py), the omitted Project item test (the `collect` cross-check), both live-claim `start` tests (verbs.py), and the `record` fallback test (records.py). The fake for the reader test answers the way GitHub was measured to: an ETag over the body only, and no Link on a 304.
- Live and read-only against jmitchel3/kanbanlan: `OMITTED_ITEMS_QUERY` on #103's issue id returned `PVTI_lAHOABXRhs4BeiXUzhGBMAE` on `PVT_kwHOABXRhs4BeiXU` for 1 GraphQL point. A real Project read with #117 removed by hand had #117 restored by the cross-check (67 of 67 items, 1 point). A REST queue read run from a copy of the 2026-11-04 cache matched both `gh issue list` and the Project: no Project issue was missing. No cards were created; no lifecycle commands ran against GitHub.

## Delivered result

A card that GitHub's Project items connection leaves out, or that falls on a page after a full last page, now reaches the snapshot. `record` reads a missing card live. `start` cannot let a session take a second card because its existing claim is missing from a stale or incomplete local view. Of the cards from that incident, only #103 was missing; none are missing now.

Follow-up, outside this card: peer repositories on a shared Project are not cross-checked. A card the items connection omits in a peer repository can still be missing from project-scope `overlap`.
