# Allow explicit repository routing when capturing into a shared Project

- Kanbanlan: `KBL-RMQ23377BBABNDRQZ6WUUQ27VM`
- Canonical home: `github`
- Canonical request: [#35](https://github.com/jmitchel3/kanbanlan/issues/35)

## Request

## Outcome

A user can capture a new request into the correct implementation repository while keeping it on the currently configured shared GitHub Project, even when the command is invoked from another bound repository or a client-level workspace.

## Observed need

`capture` always calls the configured provider, and the GitHub provider always creates the issue in `config.repository`. During the Prévenir setup, an HSA/FSA website request was captured in the automation repository because the website repository had not yet been initialized. The Project was correct but the code-owning repository was not, so the website queue cannot see or claim that card.

## Acceptance criteria

- `kanbanlan capture` accepts an explicit target such as `--repository OWNER/REPO`, with the current configured repository remaining the default.
- The target repository is validated before issue creation: same GitHub host, accessible, compatible with the configured canonical home, and linked or linkable to the selected Project.
- Required status and priority labels are verified or safely provisioned before creating the issue.
- One Kanbanlan ID is attached, the issue is added to the configured Project, and initial Inbox state reconciles in the target repository.
- Success JSON includes repository, provider reference, Kanbanlan ID, and canonical URL.
- Failures after issue creation report the exact created URL and a safe recovery command; no second request is created on retry.
- The CLI never silently guesses a target repository from title or body semantics. A future recommender may suggest a destination, but capture requires repository context or an explicit target.
- Tests cover default routing, explicit routing, inaccessible or unlinked targets, same Project across repositories, label preflight, and partial failure.
- Generated documentation explains how client-level Projects and repository-owned requests interact.

## Boundaries

This card routes new requests only. Moving an existing request without changing its Kanbanlan identity is a separate feature. It does not add semantic classification, create a second Project, or make lifecycle mutations project-wide.

## Decisions

Routing is explicit or absent. `--repository` names the target and the
configured repository is the default; nothing is inferred from the title or
body. `_capture_target` is the only place a destination is decided, and it
returns the configured repository unless the caller named a different one.

Everything that can fail happens before the request exists.
`prepare_capture_target` resolves the repository, reads the Project's linked
repositories, links the target when needed, and provisions the workflow status
and priority labels. Creation runs only after all of that succeeds, so an
inaccessible or unlinkable target leaves nothing behind.

Preflight reads only what it needs. `PROJECT_REPOSITORIES_QUERY` fetches the
Project identity and its linked repositories without the paginated item read,
because preflight runs before any request exists and should not pay for the
board.

Host compatibility is enforced by the target parser rather than by a probe.
`normalize_repository_target` accepts `OWNER/REPO` verbatim and accepts a URL
only when its host matches `config.hostname`, because a request on another
GitHub host is not addressable by the same authenticated client.

A routed request is finished, not reconciled. Repository-scoped reconciliation
cannot see a peer repository's issue, and running it would also mean asserting
authority over a repository this session does not own. `capture` therefore
reads the Project scope once, resolves the new request by its Kanbanlan ID, and
sets exactly that request's status label and Project status. The default local
path keeps the existing full `apply_reconciliation` behavior unchanged.

Retry is never the recovery. A second `capture` mints a new Kanbanlan ID and
creates a second request, so the post-creation failure message says so
explicitly, names the exact created URL, and points at
`kanbanlan reconcile --apply` in the repository that owns the request, which is
idempotent and creates nothing.

Repository routing is a declared capability. `ProviderCapabilities` gains
`repository_routing`, so a future canonical home that cannot route says so
instead of failing partway through a creation.

Mutations became repository-addressable rather than implicitly local.
`create_issue`, `ensure_labels`, `set_issue_status_label`, `comment_issue`, and
`ensure_request_identity` all take an optional `repository`, resolved through a
single `_repository` helper that defaults to the configured repository.

## Verification

`uv run pytest` (218 passed, 28 subtests), `uv run ruff check .`,
`uv run ruff format --check .`, and `uv build` all pass.

`tests/test_capture_routing.py` covers the acceptance criteria:

- `normalize_repository_target` accepts `OWNER/REPO` and same-host URLs in
  HTTPS, SSH, and `git@` forms, and refuses another host or a malformed value;
- an already linked target is not relinked but still has labels provisioned,
  and an unlinked target is linked to the configured Project;
- an inaccessible target and an unlinkable target both fail with no issue
  command issued at all;
- capture defaults to this repository, naming this repository explicitly takes
  the ordinary path, and a same-host URL target is accepted;
- a routed request is created in the target, read back at Project scope, and
  set to `status:intake` and Inbox in the repository that owns it, without
  refreshing the repository-scoped cache;
- success JSON carries repository, Kanbanlan ID, qualified provider reference,
  and canonical URL;
- a failure after creation names the created URL, says not to run capture
  again, and names the repair command and repository;
- a canonical home without `repository_routing`, and a target on another host,
  are both refused before anything is created.

## Delivered result

`kanbanlan capture --repository OWNER/REPO` creates a request in the repository
that will implement it while keeping the configured shared Project, so a
client-level Project can hold requests owned by several repositories and each
repository's queue sees its own work.

The target is validated and prepared before creation through
`GitHub.prepare_capture_target`: accessible, on the configured host, linked to
the Project (linking it when needed), and carrying the workflow status and
priority labels. The new request keeps one Kanbanlan ID, joins the Project, and
reaches Inbox in its own repository. Success JSON reports `repository`,
`kanbanlan_id`, `provider_ref`, `canonical_url`, and `routed`.

Supporting changes: `normalize_repository_target` in `config.py`,
`PROJECT_REPOSITORIES_QUERY`, a `repository` keyword on the provider's
creation, comment, status, and identity operations, and
`ProviderCapabilities.repository_routing`.

The generated workflow document and README explain how a client-level Project
and repository-owned requests interact, and that capture never guesses a
destination.

Not included, by boundary: moving an existing request is a separate outcome
(`KBL-Z4DON6QZOZB4NB56C6ZXITKH3M`). No semantic classification, no second
Project, and no lifecycle mutation becomes project-wide.
