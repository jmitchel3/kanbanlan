# release: prepare Kanbanlan 0.11.1

- Kanbanlan: `KBL-5CHR36VKKJAO7J6LKCC6SC5QQM`
- Canonical home: `github`
- Canonical request: [#74](https://github.com/jmitchel3/kanbanlan/issues/74)

## Request

## Outcome

Kanbanlan 0.11.1 is prepared on main and published, shipping #33 (config read from the primary checkout in linked worktrees).

## Acceptance criteria

- [x] Version agrees on 0.11.1 in pyproject.toml, src/kanbanlan/__init__.py, and uv.lock; SECURITY.md already names the 0.11.x line.
- [x] Tests, ruff, and uv build pass.
- [ ] v0.11.1 published to PyPI.

## Decisions

- Patch release: #33 (config read from the primary checkout in linked
  worktrees) and #76 (queued capture waits for GitHub to list the new
  request; failed-sync errors keep their message) are fixes only.
- #76 was found while preparing this release: this card's own queued
  capture failed on GitHub's listing lag with an unreadable error, so the
  release was paused, the fix delivered under #75, and the release resumed.
- SECURITY.md already names the 0.11.x line, so only `pyproject.toml`,
  `src/kanbanlan/__init__.py`, and `uv.lock` move.

## Verification

- `uv run pytest -q`: 407 passed, 37 subtests; `uv run ruff check .` clean.
- `uv build`: `kanbanlan-0.11.1.tar.gz` and `kanbanlan-0.11.1-py3-none-any.whl`.
- `uv run kanbanlan --version`: `kanbanlan 0.11.1`.

## Delivered result

Kanbanlan 0.11.1 carries #33 and #76. Publishing the v0.11.1 GitHub release
ships it to PyPI.
