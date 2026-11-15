# Release Kanbanlan 0.13.2

- Kanbanlan: `KBL-YGAGWPTHQ5E4HMXPWASFXZ53HM`
- Canonical home: `github`
- Canonical request: [#133](https://github.com/jmitchel3/kanbanlan/issues/133)
- Pull request: [#134](https://github.com/jmitchel3/kanbanlan/pull/134)

## Request

Prepare and publish Kanbanlan 0.13.2 with the push-access account fix (#132).

## Outcome

0.13.2 is tagged, released on GitHub, and published to PyPI.

## Acceptance criteria

- [x] Package, source, and lockfile versions agree on 0.13.2.
- [x] Tests, lint, formatting, and build pass.

## Decisions

- 0.13.2 is a patch release: one bug fix, no interface changes.
- Includes #132: when no binding, owner match, or single login decides, Kanbanlan probes which logged-in accounts can push to the repository and picks gh's active account if it can, else the only one that can; anything ambiguous or unanswerable is an error naming the accounts.
- Publishing workflow unchanged (OIDC Trusted Publishing on GitHub Release).

## Verification

- `uv run pytest`: 553 passed (103 subtests); ruff check and format clean; `uv build` clean.
- `uv run kanbanlan --version`: `kanbanlan 0.13.2`.

## Delivered result

Version bumped in `pyproject.toml`, `src/kanbanlan/__init__.py`, and `uv.lock`; release record generated from this PR body.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
