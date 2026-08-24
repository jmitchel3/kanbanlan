# Add a close command for terminal request outcomes

- Kanbanlan: `KBL-NRTORV52RBHKXBIYZHZJM2PCHU`
- Canonical home: `github`
- Canonical request: [#54](https://github.com/jmitchel3/kanbanlan/issues/54)

## Request

## Outcome

`kanbanlan close <request> --reason ...` drives a request to its terminal state
from the CLI, so Done is reachable without leaving Kanbanlan for `gh issue close`.

Today the only path to Done is the canonical request being closed elsewhere
(usually a merged pull request), which leaves no supported command for
won't-do, duplicate, obsolete, or delivered-without-a-PR outcomes.

## Scope

- `close` command: resolve the request, refuse when a linked pull request is
  still open (unless forced), release any active claim, close the canonical
  request with a completed or not-planned reason, and set the projection to Done.
- `CoordinationProvider.close_request` plus the GitHub implementation.
- Session activity recording for the `close` action.
- Docs: README command table and docs/workflow/kanbanlan.md lifecycle.
- Tests covering the happy path, the open-pull-request guard, the active-claim
  release, and the already-closed case.

## Decisions

- Closing is a first-class lifecycle command rather than a reconcile side
  effect. Before this, the only supported route to Done was GitHub closing the
  issue itself (usually through a merged pull request's closing reference), so
  every other terminal outcome (delivered without a pull request, duplicate,
  obsolete, will-not-build) forced an operator out to `gh issue close` and left
  the claim ledger and the projection to be repaired by the next `reconcile`.
- `close` refuses while the request has a linked open pull request, because
  merging that pull request is the closing path and closing underneath it
  detaches the delivery record from the outcome. `--force` is the escape hatch
  for a pull request that will never merge, mirroring how `rehome --apply` gates
  its own destructive step.
- The command releases an active claim before closing rather than requiring a
  separate `release`. A closed request cannot have an owner, and the RELEASED
  comment is what ends a claim in the ledger, so emitting it here keeps
  `active_claim` correct for anything that reads history after the fact. The
  released session is echoed in the result so an operator sees whose claim ended.
- Close reasons are the two GitHub records `completed` and `not planned`, mapped
  through `CLOSE_REASONS` in `kanbanlan.github` and selected by `--not-planned`.
  A delivered outcome and a dropped one need to stay distinguishable on the
  canonical request; free-text prose in the CLOSED comment cannot carry that.
  The provider rejects any other reason before running `gh`.
- After closing, the command sets the state it already knows is correct (no
  status label, projection Done) instead of leaving a drift for the next
  `reconcile`. This is exactly `expected_state`'s existing rule for a closed
  request, so the command and the reconciler cannot disagree. `_set_state` now
  accepts `label=None`, which `set_issue_status_label` already handled as
  "remove every status label".
- `already closed` is an error rather than a silent success, and it names
  `kanbanlan reconcile --apply` because that is the command that settles a
  projection which lags an externally closed request.
- `CoordinationProvider` gained `close_request` plus a `request_closing`
  capability flag, matching the existing `request_rehoming` pattern, so a future
  non-GitHub canonical home can refuse the command instead of failing partway.

## Verification

- `uv run pytest -q`: 302 passed, 37 subtests. `tests/test_close.py` adds 12
  tests covering the completed and not-planned paths, the claim release and its
  RELEASED comment, the open-pull-request refusal and its `--force` override,
  the already-closed refusal, a canonical home without `request_closing`,
  parser flag handling, the `gh issue close` argv the GitHub provider builds,
  its rejection of an unsupported reason, and `expected_state` settling a closed
  request at Done with no status label.
- `uv run ruff check src tests` and `uv run ruff format --check src tests`: clean.
- `uv build`: wheel and sdist built.
- Live verification against the real board, on scratch card
  jmitchel3/kanbanlan#55 (`KBL-AZXJGI7MRREDXL6BOXTYKRDGCQ`, captured for this
  purpose): `kanbanlan --json close ... --not-planned` returned
  `close_reason: not_planned` and `status: Done`; the issue is `CLOSED` with
  `stateReason: NOT_PLANNED`, retains only `priority:p3` (its status label was
  removed), and its Project item reads Done. The issue carries the
  `CLOSED: <timestamp> — <reason>` comment and a `close (Inbox -> Done)`
  activity entry, which `kanbanlan sessions` lists with its resume command. A
  second `close` on the same request errored with the already-closed message,
  and a following `kanbanlan reconcile` reported no drift.
- Live verification of the open-pull-request guard: with pull request #56 open
  against this request, `kanbanlan close` refused and named
  `github:jmitchel3/kanbanlan#56`.

## Delivered result

`kanbanlan close <request> --reason ...` ends a request from the CLI: it
releases any active claim, closes the canonical request as completed or, with
`--not-planned`, as dropped, comments the reason, and settles the projection at
Done with no status label. It refuses a request that already closed or that
still has a linked open pull request, the latter overridable with `--force`.
`CoordinationProvider.close_request` and the `request_closing` capability carry
the operation at the provider boundary, and the GitHub implementation maps the
two supported reasons onto `gh issue close --reason`. README, the generated
workflow doc, and the generated agent instruction block document the terminal
outcomes that do not come from a merge.
