# release: prepare Kanbanlan 0.10.0

- Kanbanlan: `KBL-L6RNYE22DVHRNI6XMWWXBWX3OE`
- Canonical home: `github`
- Canonical request: [#57](https://github.com/jmitchel3/kanbanlan/issues/57)

## Request

## Outcome

Kanbanlan 0.10.0 is prepared on `main`: the version is bumped in
`pyproject.toml`, `src/kanbanlan/__init__.py`, `uv.lock`, and the SECURITY.md
supported release line, ready for a tagged GitHub release to publish to PyPI.

## Scope

- Carries #54 (`kanbanlan close`), the only change delivered to `main` since
  0.9.0.
- Minor rather than patch: `close` is new user-facing functionality, and the
  provider protocol gained `close_request` plus a `request_closing` capability.
- No behavior changes in this request beyond the version bump.

## Decisions

- Minor rather than patch. `kanbanlan close` (#54, merged as 600ce33) is new
  user-facing functionality, and the provider protocol grew `close_request` plus
  a `request_closing` capability flag, matching the repository's precedent of
  bumping the minor for functionality before 1.0.
- The version lives in four places that must move together: `pyproject.toml`,
  `src/kanbanlan/__init__.py` (the literal the CLI's `--version` and the update
  notifier read), `uv.lock` (regenerated with `uv lock`), and the supported
  release line in SECURITY.md. The release workflow refuses to publish when the
  tag does not match `uv version --short`, so a missed bump fails at the tag
  rather than shipping a mislabeled wheel.
- Publishing stays a human step: `.github/workflows/release.yaml` runs on a
  published GitHub release, so this request stops at a merged version bump.

## Verification

- `uv run pytest -q`: 302 passed, 37 subtests.
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `uv build`: built `kanbanlan-0.10.0.tar.gz` and
  `kanbanlan-0.10.0-py3-none-any.whl`.
- `uv run kanbanlan --version`: `kanbanlan 0.10.0`.

## Delivered result

Kanbanlan 0.10.0 is prepared on `main`, carrying `kanbanlan close` as the only
change since 0.9.0. Tagging `v0.10.0` and publishing the GitHub release ships it
to PyPI.
