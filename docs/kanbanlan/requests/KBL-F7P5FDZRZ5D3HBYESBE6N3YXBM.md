# Upgrade astral-sh/setup-uv to v9

- Kanbanlan: `KBL-F7P5FDZRZ5D3HBYESBE6N3YXBM`
- Canonical home: `github`
- Canonical request: [#8](https://github.com/jmitchel3/kanbanlan/issues/8)

## Request

## Outcome

Upgrade CI and release workflows from astral-sh/setup-uv 8.1.0 to 9.0.0 while retaining the pinned uv tool version and accepting the new non-pruning cache default.

## Acceptance criteria

- [x] CI and release workflows use the immutable setup-uv v9.0.0 commit.
- [x] The pinned uv version, cache enablement, tests, lint, formatting, and build behavior remain intact.
- [x] The cache-retention tradeoff and verified delivery are documented.

## Decisions

- Pin setup-uv v9.0.0 by immutable commit
  `c771a70e6277c0a99b617c7a806ffedaca235ff9` in both workflows.
- Keep `uv` pinned to 0.11.16 and keep setup-uv caching enabled.
- Accept setup-uv v9's new `prune-cache: false` default instead of restoring the
  old pruning behavior. Retaining the small cache avoids repeated downloads and
  is the upstream-recommended default.

## Verification

- The pull request's Python 3.11, 3.12, 3.13, and 3.14 jobs loaded the v9 commit,
  installed uv 0.11.16, and passed.
- `uv run pytest -q` — 70 tests and 4 subtests passed after updating the
  branch to current `main`.
- `uv run ruff check .` — passed.
- `uv run ruff format --check .` — all 37 files formatted.
- `uv build` — source distribution and wheel built successfully in CI.
- `git diff --check` — passed.
- CI logs confirmed `prune-cache: false`. The retained cache was approximately
  12.5 MB for this repository, an acceptable tradeoff for warmer installs.

## Delivered result

CI and PyPI release jobs now install uv through the immutable setup-uv v9.0.0
commit. The selected uv version and all workflow steps remain unchanged; cache
contents are retained under v9's new default.
