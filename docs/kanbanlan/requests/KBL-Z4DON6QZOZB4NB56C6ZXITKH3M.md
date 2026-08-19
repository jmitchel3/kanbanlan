# Rehome a request across repositories without changing its Kanbanlan identity

- Kanbanlan: `KBL-Z4DON6QZOZB4NB56C6ZXITKH3M`
- Canonical home: `github`
- Canonical request: [#36](https://github.com/jmitchel3/kanbanlan/issues/36)

## Request

## Outcome

An operator can move a canonical request to the repository where its independently reviewable result belongs while preserving the immutable Kanbanlan ID, Project status, audit history, and discoverability.

## Observed need

A website HSA/FSA request was captured in an automation repository before the website repository joined the shared Project. Leaving it there makes repository-scoped `status`, `next`, and `claim` present it to the wrong implementation flow. Creating a replacement would fragment history and violate the portable-identity goal.

## Acceptance criteria

- Add a command such as `kanbanlan rehome KBL-... --repository OWNER/REPO` with a non-mutating plan by default and an explicit `--apply` mutation.
- Resolve the source by Kanbanlan ID and preflight the target repository, Project binding, permissions, workflow labels, and provider transfer support before changing anything.
- Preserve the exact Kanbanlan ID and canonical issue discussion or audit history when the provider supports issue transfer.
- Reconcile the new provider reference, issue number, canonical URL, Project item, status, priority, and session history after transfer without creating a duplicate request.
- Refuse or require an explicit safe sequence for active claims, open linked pull requests, repository-specific branches, worktrees, milestones, or labels that cannot transfer cleanly.
- Same-host and same-canonical-Project moves are supported first. Unsupported cross-host or cross-provider moves fail before mutation.
- If the provider applies the transfer but a later reconciliation step fails, report the new canonical URL and a retryable recovery command. Never retry by creating a replacement issue.
- JSON output includes old and new provider references plus every preserved or dropped field.
- Tests cover dry run, successful GitHub transfer, number collision, label setup, active-claim refusal, partial failure, cache refresh, and lookup by the original Kanbanlan ID.

## Boundaries

This moves the canonical request, not its implementation branch, worktree, commits, or pull requests. It does not infer the destination semantically and does not make automatic background transfers.

## Decisions

The plan is the default and the mutation is opt-in. `kanbanlan rehome` prints
what it would do and changes nothing; `--apply` performs the move. A move is
the one lifecycle operation that cannot be undone by rerunning it, so seeing it
first is worth a second command.

Planning is separated from preparation in the provider.
`inspect_repository_target` reads accessibility, Project linkage, and missing
workflow labels without changing anything, and `prepare_repository_target`
performs the linking and label provisioning. `prepare_capture_target` from the
previous request became a thin alias, so capture and rehome share one
definition of a prepared target.

The source is resolved across the Project, not locally. A request that belongs
in another repository has often already landed in a peer one, which is the
whole reason to move it, so `rehome` reads at project scope and resolves by
Kanbanlan ID.

Blockers describe the safe sequence rather than just refusing. An active claim
names the session holding it and the `kanbanlan release` command that frees it;
a linked open pull request says to merge or close it first and states that a
rehome moves the request, not its implementation. A closed request and a
destination equal to the current repository are refused outright.

Untransferable fields are named before the move, not discovered after it.
GitHub drops a milestone across repositories and drops labels the target does
not have, so the plan lists them explicitly and the applied result repeats them
as warnings. Workflow `status:` and `priority:` labels are excluded from that
list because preparation provisions them in the target first. `milestone` was
added to `PROJECT_QUERY` and to the snapshot so this report is real rather than
speculative.

Reconciliation after the transfer resolves by Kanbanlan ID, which the transfer
preserved. That makes the repair idempotent: rerunning `rehome --apply` after a
partial failure finishes the move instead of creating a replacement request.
The failure message says exactly that and names the new canonical URL, because
the instinct after a failed move is to recreate the request.

Rehoming is a declared capability. `ProviderCapabilities.request_rehoming` lets
a canonical home that cannot transfer an issue refuse before mutation rather
than partway through one.

The host check happens in the target parser, before any read. A cross-host
destination fails before the Project is even fetched.

`COMMAND_NAMES` had fallen out of step with the parser: `overlap` from
`KBL-3G2ZFMMHDNHWDMWBQ2SSGUQ5UU` was missing, and `rehome` would have been
missing too. Both are registered now, and a test compares the tuple against the
parser's own choices so it cannot drift again.

## Verification

`uv run pytest` (238 passed, 28 subtests), `uv run ruff check .`,
`uv run ruff format --check .`, and `uv build` all pass.

`tests/test_rehome.py` covers the acceptance criteria:

- a clean plan preserves identity, discussion, and session history, and reports
  the target setup it would perform;
- a milestone and non-workflow labels are reported as dropped while `status:`
  and `priority:` labels are not;
- an active claim, a linked open pull request, a closed request, and a
  destination equal to the current repository each block the move, and a
  request without a portable identity is refused;
- the default run is a plan that mutates nothing and a blocked plan exits 2;
- `--apply` prepares the target, transfers the request, sets its status label
  in the new repository, restores its Project status, and refreshes the shared
  cache;
- applied JSON names both provider references, the new number and URL, the
  preserved fields, and every dropped field;
- the moved request is still found by its original Kanbanlan ID, and an
  identically numbered issue already in the target does not confuse the move;
- a failure after the transfer names the new location, states that the request
  was not duplicated, gives a retryable command, and creates no replacement;
- `--apply` refuses a blocked move, a cross-host destination fails before any
  read or mutation, and a canonical home without `request_rehoming` is refused;
- `COMMAND_NAMES` matches the parser's registered commands.

## Delivered result

`kanbanlan rehome KBL-... --repository OWNER/REPO` moves a canonical request to
the repository where its independently reviewable result belongs, preserving
the immutable Kanbanlan ID, the issue discussion and session history, and the
Project status and priority. The plan is the default; `--apply` performs the
move.

Preflight covers target accessibility, Project binding, workflow labels, host
compatibility, and provider transfer support, and every one of those failures
happens before any mutation. Active claims, linked open pull requests, closed
requests, and no-op destinations block the move and name the safe sequence that
unblocks it. JSON reports both provider references, the new number and URL, the
preserved fields, and every dropped field.

Supporting changes: a new `kanbanlan/rehome.py` planning module,
`GitHub.inspect_repository_target`, `GitHub.prepare_repository_target`,
`GitHub.repository_labels`, `GitHub.transfer_request`,
`ProviderCapabilities.request_rehoming`, `milestone` in the Project query and
snapshot, and `overlap` and `rehome` added to `COMMAND_NAMES` with a test that
keeps that list in step with the parser.

Not included, by boundary: implementation branches, worktrees, commits, and
pull requests are not moved, destinations are never inferred, and no transfer
happens in the background.
