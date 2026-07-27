# Ignore generated Kanbanlan worktree checkouts

- Kanbanlan: `KBL-KSVGOE7NINHHBOFBEXBK4P7QO4`
- Canonical home: `github`
- Canonical request: [#9](https://github.com/jmitchel3/kanbanlan/issues/9)

## Request

## Outcome

Kanbanlan-created worktrees under `/.worktrees/` never dirty the primary
checkout, both in this repository and in repositories scaffolded by
`kanbanlan init`.

## Acceptance criteria

- [x] Repository scaffolding idempotently adds both `/.cache/kanbanlan/` and
      `/.worktrees/` to `.gitignore` while preserving custom content.
- [x] This repository ignores its existing `.worktrees` directory.
- [x] Tests cover first-run, migration from the cache-only entry, and repeat
      scaffolding.
- [x] README managed-file documentation describes both local ignore entries.
- [x] The complete test, lint, format, lock, build, distribution, and YAML
      verification gates pass before release.

## Out of scope

Automatically deleting or pruning existing worktrees.

## Decisions

- Treat `/.cache/kanbanlan/` and `/.worktrees/` as the two managed local-state
  entries. Append only missing entries, preserve unrelated lines, and normalize
  the legacy cache-only heading without replacing a custom `.gitignore`.
- Keep worktree deletion and pruning out of scope; ignoring a local checkout is
  safe and reversible, while removing one requires separate ownership checks.
- Release as `0.4.0`: v0.3.0 is already immutable on PyPI, and the background
  reconciliation worker merged since that release is meaningful new behavior.

## Verification

- `uv lock --check` — resolved 8 locked packages with no changes.
- `uv run pytest` — 100 tests and 4 subtests passed.
- `uv run ruff check .`, `uv run ruff format --check .`, and `git diff
  --check` — passed.
- `uv build` produced the 0.4.0 source distribution and wheel. Wheel metadata
  reports `Version: 0.4.0`; both archives contain the worker, registry,
  scaffold, documentation, tests, and request record expected for this release.
- Ruby Psych parsed every workflow and issue-form YAML file successfully.
- A repository-wide credential-pattern scan found no token, access-key, or
  private-key material in tracked or untracked release files.
- A local-only initialization smoke test generated both ignore entries; `git
  check-ignore -v` matched `.worktrees/example` and the coordination cache.
- Repeating the same initialization reported every managed file, including
  `.gitignore`, as unchanged.
- `kanbanlan --version`, `uv version --short`, `pyproject.toml`,
  `src/kanbanlan/__init__.py`, and `uv.lock` all report `0.4.0`.

## Delivered result

Initialized repositories now ignore both Kanbanlan's shared cache and generated
worktree directory without disturbing custom ignore rules. The repository's own
existing worktrees stop dirtying the primary checkout after delivery, and the
package is prepared as version 0.4.0 for the verified GitHub/PyPI release.
