# Introduce portable Kanbanlan identity and provider-neutral core

- Kanbanlan: `KBL-OZ6PYYZBYJATTO2KTJFQ4OHJJM`
- Canonical home: `github`
- Canonical request: [#3](https://github.com/jmitchel3/kanbanlan/issues/3)

## Request

## Outcome

Kanbanlan remains fully GitHub-backed today while its identity, lifecycle, repository records, CLI references, and provider contracts become portable to future canonical kanban homes.

## Acceptance criteria

- [x] New requests receive an immutable Kanbanlan ID persisted in GitHub and snapshots.
- [x] Commands resolve both Kanbanlan IDs and GitHub issue numbers.
- [x] Workflow logic depends on a provider-neutral contract, with GitHub as the first implementation.
- [x] Completed work can leave a durable per-request Markdown record in the repository without committing volatile board state.
- [x] Agent-facing read commands support stable JSON output and structured failures where practical.
- [x] Existing schema-v1 configuration and GitHub behavior remain compatible.
- [x] Contract, migration, lifecycle, and CLI tests pass.

## Scope boundaries

No Linear or Asana clients, webhooks, OAuth hosting, multi-master synchronization, or server-side database.

## Likely touchpoints

Configuration and schema; provider interfaces; GitHub adapter; snapshot normalization; lifecycle reconciliation; CLI parsing/output; scaffolding and documentation; tests.

## Decisions

- A Kanbanlan ID is `KBL-` plus a 128-bit random value encoded as 26 base32
  characters. It is generated without a central service and never derived from
  a provider identifier.
- GitHub issue bodies carry both a machine-readable HTML marker and a visible
  `Kanbanlan:` line. Reconciliation assigns missing identities explicitly and
  reports duplicate identities without silently replacing either one.
- GitHub remains the only implemented canonical home. The CLI and reconciler
  now depend on a coordination-provider contract, and snapshots expose portable
  provider references alongside compatibility fields.
- Volatile board state remains in the canonical home and ignored local cache.
  `kanbanlan record` creates one Markdown record per request and never overwrites
  subsequent human or agent edits.
- Schema-v1 configuration remains readable; new scaffolds write schema v2 with
  separate code-host, canonical-home, and projection declarations.

## Verification

- `uv run pytest -q` — 64 tests and 4 subtests passed.
- `uv run ruff check .` — passed.
- `uv run ruff format --check .` — all 34 files formatted.
- `git diff --check` — passed.
- `uv build` — source distribution and wheel built successfully.
- Live `uv run kanbanlan reconcile` previewed the missing identity on request
  #3, and `reconcile --apply` assigned and verified this record's Kanbanlan ID.
- Live `uv run kanbanlan --json status` returned structured success output;
  resolving an unknown ID returned a structured error.
- Final live `uv run kanbanlan reconcile` reported no GitHub Issue/Project drift.

## Delivered result

Kanbanlan remains fully operational with GitHub Issues and GitHub Projects, but
new requests now have portable identities, lifecycle commands accept portable
or provider references, PRs can link by Kanbanlan ID, and completed work can be
documented in the repository. Linear/Asana adapters, mirrors, hosted webhooks,
and multi-master synchronization remain deliberately deferred.
