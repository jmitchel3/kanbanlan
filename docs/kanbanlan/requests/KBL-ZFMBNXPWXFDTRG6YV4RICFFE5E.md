# Recognize cross-repository pull requests linked to a request

- Kanbanlan: `KBL-ZFMBNXPWXFDTRG6YV4RICFFE5E`
- Canonical home: `github`
- Canonical request: [#37](https://github.com/jmitchel3/kanbanlan/issues/37)

## Request

## Outcome

A request in one repository can be reviewed and reconciled from an implementation pull request in another repository when both belong to the same delivery Project and the PR explicitly references the request or its Kanbanlan ID.

## Observed need

A Prévenir coordination request lived in `prevenir-automations`, while the implementation PR correctly lived in `prevenircardiowell.com`. The website PR used a cross-repository closing reference and carried the Kanbanlan ID, but `kanbanlan review` reported that the request had no linked open pull request. A second documentation-only PR in the issue repository was required solely to satisfy lifecycle detection.

Current behavior explains the gap: open pull requests are fetched only from `config.repository`, `_normalize_pull_request` discards closing issue references whose repository differs from that configured repository, and Project items from peer repositories are filtered from the repository snapshot.

## Acceptance criteria

- `linked_open_pull_requests` can include a PR from another repository when it explicitly closes the request by repository-qualified reference or contains the exact Kanbanlan ID.
- The normalized PR identity includes repository and a repository-qualified provider reference; same-number PRs from different repositories never collide.
- `kanbanlan review` accepts a qualifying open cross-repository PR and preserves the responsible claim or session attribution.
- Reconciliation understands open, merged, closed-unmerged, and draft cross-repository PR states without moving unrelated cards.
- Discovery is bounded to the configured Project, explicit peer repositories, or provider-native issue cross-references; it does not scan every repository owned by the account.
- A bare `Closes #34` is interpreted only in the PR repository. Cross-repository links must use an owner/repository reference or the immutable Kanbanlan ID.
- Ambiguous, duplicated, or conflicting Kanbanlan IDs fail visibly and do not associate the PR.
- Stable JSON documents repository-qualified linked PR fields.
- Tests cover PR and issue in the same repository, PR and issue in different repositories, identical issue or PR numbers across repositories, ID-only linkage, cross-repository closing linkage, drafts, merge, close without merge, and unrelated Project content.

## Boundaries

This feature recognizes explicit cross-repository delivery relationships. It does not transfer issues, move code, infer links from similar titles, or make all lifecycle operations project-wide.

## Decisions

Two routes are explicit enough to cross a repository boundary: a
repository-qualified closing reference, and a Kanbanlan ID declared in the pull
request body. Everything else stays local.

The bare-reference rule needs no check here. GitHub only resolves a closing
reference to another repository when the author qualified it; a bare
`Closes #34` in repository A always resolves to A's issue 34. Reading
`closingIssuesReferences` and linking on the reference's own repository
therefore satisfies "a bare reference is interpreted only in the PR
repository" by construction rather than by parsing prose.

Pull request discovery moved out of the scope decision. `GitHub.collect` now
reads open pull requests from every repository the Project references at either
scope, because a request here can be delivered by a pull request in a peer
repository and repository scope is exactly where `review` runs. Scope now
decides only what is *reported*: repository scope keeps a peer pull request
only while it delivers a request in the configured repository.

Discovery is bounded two ways, both Project-derived. `ProjectV2.repositories`
qualifies a linked repository before it has any card, which is how a peer
repository's first delivery is recognized; Project item content qualifies a
repository whose work is already on the board even without the link. Nothing
enumerates repositories the account owns. In a single-repository Project both
sets collapse to the configured repository, so the common case costs nothing.

A declared identity beats a mentioned one. `extract_kanbanlan_id` reads the
`Kanbanlan:` line or HTML marker that the generated pull request template
already asks for, and that declaration wins even when the body mentions other
IDs in passing. Only an undeclared body carrying several IDs is conflicting.
Without this, an ordinary "follow-up: KBL-..." sentence would silently break a
link, and the previous behavior of linking such a pull request to every ID it
mentioned was worse still.

Ambiguity is reported, not repaired. Conflicting or duplicated identities land
in a new top-level `linkage_problems` list rather than in reconciliation drift,
because drift entries are things `reconcile --apply` can fix and these are not;
emitting them as drift would make `--apply` report unresolved work forever.
`reconcile`, `overlap`, and `review` all surface the list instead.

Identity ambiguity is judged across the whole Project regardless of read scope.
A duplicated Kanbanlan ID in a peer repository makes an identity link ambiguous
here too, so `_project_identity_owners` scans every Project issue.

`linked_by` records why each link exists. A pull request can reach the same
request through both routes, and an operator resolving a surprising link needs
to know which reference caused it.

## Verification

`uv run pytest` (201 passed, 18 subtests), `uv run ruff check .`, and
`uv run ruff format --check .` all pass.

The `ProjectV2.repositories` field was confirmed against this repository's own
Project before the query depended on it:

```sh
gh api graphql -f query='query { user(login: "jmitchel3") {
  projectV2(number: 2) { repositories(first: 10) { nodes { nameWithOwner } } } } }'
```

`tests/test_cross_repository_links.py` covers the acceptance criteria:

- pull request and request in the same repository still link;
- a bare closing reference resolves in the pull request's own repository and
  cannot reach an identically numbered peer request;
- a qualified closing reference and a declared Kanbanlan ID each cross the
  boundary, and both routes are reported together when both apply;
- identical issue numbers and identical pull request numbers across
  repositories never collide;
- unrelated Project content is never associated, and a peer pull request is
  reported only while it delivers a local request;
- conflicting and duplicated identities link nothing and appear in
  `linkage_problems`, while a declared identity survives a passing mention and
  an unknown identity is simply not a link;
- reconciliation handles open, draft, merged, and closed-without-merge
  cross-repository states and moves no unrelated card;
- `review` accepts closing-reference and identity-only cross-repository pull
  requests while preserving the responsible claim, explains a refusal caused by
  ambiguity, and still refuses a request with no pull request at all.

`tests/test_project_scope.py` adds coverage for Project-bounded discovery in
both scopes and for a linked repository that has no cards yet.

## Delivered result

`linked_open_pull_requests` now includes a pull request from another repository
when it closes the request by repository-qualified reference or declares the
request's exact Kanbanlan ID, so `kanbanlan review` accepts the real
implementation pull request instead of requiring a documentation-only pull
request in the request's own repository.

Each linked entry carries `repository`, a qualified `provider_ref`, and
`linked_by`. Snapshots gain a top-level `linkage_problems` list, which
`reconcile`, `overlap`, and `review` surface. `review` reports the pull requests
that justified the move in both text and JSON.

`GitHub.collect` discovers open pull requests across repositories linked to the
Project or already carrying Project items, and `PROJECT_QUERY` reads
`ProjectV2.repositories` for that bound. Pull requests carry
`declared_kanbanlan_id`.

The generated workflow document and the README describe cross-repository
delivery, the two accepted references, the bare-reference rule, and how
ambiguity is reported.

Not included, by boundary: no issue transfer, no code movement, no inference
from similar titles, and no lifecycle operation becomes project-wide.
