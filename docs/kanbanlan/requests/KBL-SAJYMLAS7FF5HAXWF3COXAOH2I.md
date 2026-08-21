# Worker skips the verification refresh on a clean cycle

- Kanbanlan: `KBL-SAJYMLAS7FF5HAXWF3COXAOH2I`
- Canonical home: `github`
- Canonical request: [#45](https://github.com/jmitchel3/kanbanlan/issues/45)

## Request

## Outcome

The background worker stops paying for two full snapshot refreshes and two open-issue sweeps every cycle when nothing drifted.

- When plan_reconciliation reports no drift, skip the verification refresh and the second list_open_requests call.
- Verification still runs after any applied repair.
- Roughly halves steady-state GraphQL spend per registered repository.

See docs/improvements/by-fable.md (request 2).

## Decisions

- Verification moved inside the drift branch of `Worker._run_registration`
  rather than being deleted. A cycle that applied a repair still proves the
  repair took by re-reading live state; a clean cycle already proved itself
  with the read it opened with, so a second identical read only spends shared
  GraphQL points.
- The unresolved-duplicate guard and the failure backoff are untouched; the
  change affects only which cycles pay for the second read.

## Verification

- `uv run pytest`: 239 tests plus 28 subtests pass on this branch.
- `uv run ruff check .` and `uv run ruff format --check .` are clean.
- `test_successful_iteration_refreshes_plans_and_resets_health` now pins the
  clean cycle to exactly one refresh and one open-issue sweep, and the new
  `test_applied_repair_is_verified_with_a_second_refresh` pins the drift path
  to two of each with `apply_reconciliation` called once.

## Delivered result

A clean worker cycle now costs one project read plus one pull-request sweep
instead of two of each, roughly halving steady-state GraphQL spend per
registered repository. Cycles that repaired drift keep the full
apply-then-verify sequence. Part of the rate-limit remediation described in
`docs/improvements/by-fable.md`; the ensure-side degradation landed as
KBL-2FFYGZHCYNEPXPEU5D6TIHV7GM, and incremental item hydration
(KBL-ZPRKDYD3AJCKLFJCVKVBU2XK4Q) remains captured for later.
