# Release Kanbanlan 0.13.1

- Kanbanlan: `KBL-BCXY2SKRJBGFRJAMRPJ6OVCN6U`
- Canonical home: `github`
- Canonical request: [#129](https://github.com/jmitchel3/kanbanlan/issues/129)
- Pull request: [#130](https://github.com/jmitchel3/kanbanlan/pull/130)

## Request

## Outcome

<!-- Describe the independently reviewable result. -->

## Acceptance criteria

- [ ]

## Decisions

- 0.13.1 is a patch release: bug fixes only, no interface changes.
- Includes #126 (init replaces a managed block that lost its start marker instead of duplicating it), #127 (git fetch and push authenticate as the repository's bound gh account), and #128 (start no longer spends minutes pruning orphaned worktrees; it removes at most 3 per call and defers the rest to `admin cleanup`).
- Publishing workflow unchanged (OIDC Trusted Publishing on GitHub Release).

## Verification

- `uv run pytest`: 540 passed (103 subtests); ruff check and format clean; `uv build` clean.
- `uv run kanbanlan --version`: `kanbanlan 0.13.1`.
- `kanbanlan done` from this branch pushed and opened this PR with the active gh account set to a different login than the bound one, with no token override.

## Delivered result

Version bumped in `pyproject.toml`, `src/kanbanlan/__init__.py`, and `uv.lock`; release record generated from this PR body.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
