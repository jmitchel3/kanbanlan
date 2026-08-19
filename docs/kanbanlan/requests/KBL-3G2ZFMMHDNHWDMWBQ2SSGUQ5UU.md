# Add project-wide read and overlap scope for shared multi-repository Projects

- Kanbanlan: `KBL-3G2ZFMMHDNHWDMWBQ2SSGUQ5UU`
- Canonical home: `github`
- Canonical request: [#34](https://github.com/jmitchel3/kanbanlan/issues/34)

## Request

## Outcome

An operator or agent working in one repository can inspect every open request, active claim, and pull request represented by the same GitHub Project, while ordinary execution queues and lifecycle mutations remain repository-scoped by default.

## Observed need

Two Prévenir repositories are bound to one client-delivery Project. `kanbanlan status` in the website repository correctly reports an empty repository queue while the automation repository reports its own cards. That isolation is useful for `next` and `claim`, but the generated agent policy also requires checking all open cards and pull requests for semantic overlap. Today a repository cannot perform that project-wide check.

The provider already fetches every Project item. `build_snapshot` then drops issues and pull requests whose `repository.nameWithOwner` differs from `config.repository`, and open pull requests are queried only from the configured repository.

## Acceptance criteria

- Add an explicit read-only project scope, such as `kanbanlan status --project`, `snapshot --project`, or a focused `overlap` command.
- Project-scoped JSON retains repository-qualified provider references and identifies the repository on every issue and pull request.
- Same-number issues and pull requests from different repositories never collide.
- Repository-local behavior remains the default for `next`, `capture`, `claim`, `release`, `review`, `handoff`, and ordinary `reconcile`.
- Project scope includes Project items across repositories without scanning unrelated repositories in the owner account.
- Active claims, touchpoints, statuses, and linked pull requests are visible across the Project so agents can perform the overlap check required by generated instructions.
- Pagination, rate-limit reporting, empty repositories, draft items, and inaccessible repository content are covered by tests.
- Stable JSON documentation explains repository scope versus Project scope.

## Dependencies and boundaries

This is adjacent to the existing improvement suggestion for machine-checkable semantic overlap, but this card supplies the missing multi-repository data rather than implementing semantic matching. It does not mutate peer-repository issues, add automatic claim blocking, introduce multiple canonical homes, or change default queue selection.

## Decisions

Read scope is an explicit parameter, not a mode. `build_snapshot` takes
`scope="repository"` (the default) or `scope="project"`, and the resulting
document reports it as `source.scope`. Every lifecycle command keeps the
default, so a shared Project cannot change how `next`, `capture`, `claim`,
`release`, `review`, `handoff`, or ordinary `reconcile` behave.

Queue selection stays repository-local in both scopes. Project scope widens what
an agent can *see*, never what this repository may *take*. `ready_cards` and
`next_ready` filter on the item repository rather than on the scope, so a
project-scoped snapshot still answers "what is mine to claim".

Project-scoped reads are live and stay in memory. The shared cache under
`.cache/kanbanlan/` is repository-scoped by contract and is read by the worker
and by every worktree, so widening what it stores would change unrelated
behavior. `_project_snapshot` calls the provider directly and never touches the
store.

Collection is bounded by the Project, not by the account. `project_repositories`
derives targets from Project item content only, so a repository joins the read
by already being on the board. Nothing enumerates repositories the account owns.

A peer repository that cannot be read is reported, not fatal. `GitHub.collect`
records `{repository, error}` in `source.unavailable_repositories` and
continues, because the configured repository still has usable state. Failure to
read the configured repository still raises; that is not a partial answer.

Identity is repository-qualified everywhere it can collide. `provider_ref` is
`github:owner/repo#number` for issues and pull requests alike, and linked pull
requests are indexed by `(repository, number)` and `(repository, kanbanlan_id)`
rather than by number alone. `display_id` stays short (`#123`) for the
configured repository and qualifies (`owner/repo#123`) for a peer, so terminal
output is unambiguous without becoming noisy in the common case.

A bare issue number is defined as a local reference. `KanbanlanRequest.matches`
takes the snapshot's repository and refuses to match a bare number against a
peer request, so `resolve_request_item(snapshot, "34")` cannot silently pick a
peer repository's issue 34. Peer requests are addressed by Kanbanlan ID or by a
qualified reference.

Cross-repository *linkage* is deliberately excluded. Linked pull requests still
require the pull request and the issue to share a repository. Recognizing an
explicit cross-repository delivery relationship is a separate outcome
(`KBL-ZFMBNXPWXFDTRG6YV4RICFFE5E`); this request supplies the multi-repository
data that work needs.

Snapshot `SCHEMA_VERSION` moved from 2 to 3. The linked-pull-request index keys
changed, so an existing cached snapshot is stale rather than merely older; the
bump makes every worktree refresh instead of reading a document built under the
previous linkage rules.

## Verification

`uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` all
pass. `tests/test_project_scope.py` covers the acceptance criteria directly:

- repository scope drops peer content, project scope keeps it, and draft items
  are skipped in both;
- identically numbered issues and pull requests in different repositories keep
  distinct `provider_ref`, `display_id`, and linked pull requests;
- a bare number resolves only in the configured repository, while a qualified
  reference resolves a peer;
- project-scoped queue selection still returns only local Ready cards;
- collection reads every Project repository and no others, follows both Project
  and pull request pagination, and reports the scarcest `rateLimit.remaining`;
- a repository with no open pull requests is not an error;
- an unreadable peer repository is reported in
  `source.unavailable_repositories` while an unreadable configured repository
  still raises;
- `status --project`, `snapshot --project`, and `overlap` are read-only, emit
  repository-qualified stable JSON, surface unreadable peers, and refuse a
  canonical home whose capabilities exclude project scope.

## Delivered result

An explicit read-only project scope. `kanbanlan overlap` lists every open
request and open pull request across the Project with claims, touchpoints,
statuses, and linked pull requests, which is the check the generated agent
policy already required but could not perform. `kanbanlan status --project`
adds per-repository board counts and `kanbanlan snapshot --project` prints the
project-scoped document.

Supporting changes: `build_snapshot` and `GitHub.collect` take a scope,
`ProviderCapabilities` gains `project_scope`, snapshots carry
`source.scope`, `source.repositories`, and `source.unavailable_repositories`,
and issues and pull requests carry `repository` plus repository-qualified
references. The generated workflow document, `AGENTS.md`, `CLAUDE.md`, and the
README explain repository scope versus Project scope.

`_write_json` now resolves `sys.stdout` per call instead of binding it at import
time, so redirected output is honored.

Follow-up, tracked separately: cross-repository pull request recognition
(`KBL-ZFMBNXPWXFDTRG6YV4RICFFE5E`), explicit repository routing on capture
(`KBL-RMQ23377BBABNDRQZ6WUUQ27VM`), and rehoming a request
(`KBL-Z4DON6QZOZB4NB56C6ZXITKH3M`).
