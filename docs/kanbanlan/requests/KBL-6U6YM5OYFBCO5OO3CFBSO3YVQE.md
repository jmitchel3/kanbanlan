# release: prepare Kanbanlan 0.11.0

- Kanbanlan: `KBL-6U6YM5OYFBCO5OO3CFBSO3YVQE`
- Canonical home: `github`
- Canonical request: [#72](https://github.com/jmitchel3/kanbanlan/issues/72)

## Request

## Outcome

Kanbanlan 0.11.0 is prepared on main (pyproject.toml, src/kanbanlan/__init__.py, uv.lock, SECURITY.md supported line) and published as a GitHub release, which ships it to PyPI.

## Scope

Carries #60 (cleanup command), #62 (cleanup recoverability fix), #68 (concurrent reads, coalesced refreshes), #69 (explicit gh account binding), #71 (instant lifecycle commands with write-behind sync).

## Acceptance criteria

- [x] Version agrees on 0.11.0 in all four places.
- [x] Tests, ruff, and uv build pass.
- [ ] v0.11.0 GitHub release published and the PyPI workflow succeeds.

## Decisions

- Minor rather than patch: `cleanup`, `account`, and `sync` are new
  commands, and lifecycle commands changed behavior (they now queue and
  return, with `KANBANLAN_WRITE_BEHIND=0` restoring the old behavior).
- The version moves together in `pyproject.toml`, `src/kanbanlan/__init__.py`,
  `uv.lock` (regenerated with `uv lock`), and the SECURITY.md supported line;
  the release workflow refuses a tag that does not match `uv version --short`.
- Unlike 0.10.0, publishing is part of this request: Justin asked for the
  release to be cut, so the v0.11.0 GitHub release is created after merge.

## Verification

- `uv run pytest -q`: 397 passed, 37 subtests.
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `uv build`: built `kanbanlan-0.11.0.tar.gz` and
  `kanbanlan-0.11.0-py3-none-any.whl`.
- `uv run kanbanlan --version`: `kanbanlan 0.11.0`.
- This request was itself captured, triaged, and claimed through the new
  write-behind path (`capture` returned in 0.17s).

## Delivered result

Kanbanlan 0.11.0 carries #60 (cleanup), #62 (cleanup recoverability fix),
#68 (concurrent reads and coalesced refreshes), #69 (explicit gh account
binding), and #71 (instant lifecycle commands with write-behind sync).
