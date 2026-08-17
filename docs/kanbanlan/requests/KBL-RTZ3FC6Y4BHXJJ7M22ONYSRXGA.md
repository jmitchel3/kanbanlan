# Release Kanbanlan 0.7.0

- Kanbanlan: `KBL-RTZ3FC6Y4BHXJJ7M22ONYSRXGA`
- Canonical home: `github`
- Canonical request: [#30](https://github.com/jmitchel3/kanbanlan/issues/30)

## Request

## Outcome

Kanbanlan 0.7.0 is published to PyPI and tagged on GitHub.

## Scope

Two delivered requests are on `main` and unreleased:

- #28 `fix: quote command arguments in CLI error hints` (KBL-QTQV5A4FXZFQTJ3R7JFW5XSKMA)
- #29 `feat: retry transient GitHub 5xx responses on read commands` (KBL-N3ZOODQUYJBJ3N4GMIWP65AGA4)

## Version

0.7.0, not 0.6.1. #29 is a feature, and repository precedent is a minor bump for
a feature: #21 (`feat: update existing init configuration in place`) shipped as
0.6.0.

## Acceptance

- `pyproject.toml`, `src/kanbanlan/__init__.py`, and `uv.lock` report 0.7.0.
- `SECURITY.md` names the `0.7.x` release line.
- Tag `v0.7.0` matches `uv version --short`, which the release workflow enforces.
- The published GitHub Release triggers `.github/workflows/release.yaml` and
  PyPI serves 0.7.0.

## Decisions

Released as 0.7.0 rather than 0.6.1. #29 adds a feature (opt-in retry), and the
repository precedent is a minor bump for a feature: #21
(`feat: update existing init configuration in place`) shipped as 0.6.0.

Used `uv version 0.7.0`, which updates `pyproject.toml` and `uv.lock` together.
`src/kanbanlan/__init__.py` and `SECURITY.md` are not derived from either, so
they were edited directly. This matches the four files changed by the 0.6.0
release commit in #23.

Resolving #28 against #29 happened before this request, on the #29 branch. Both
had added tests at the same anchor in `tests/test_cli.py`; all three tests were
kept. One behavior interaction is worth recording: a `gh` failure reporting
`unknown owner type` now receives the new owner-resolution hint rather than the
generic "run this directly" text, while the shell-quoted command still appears
in the `command failed (...)` line. Both tests assert against that combined
result.

## Verification

- `uv version --short` reports `0.7.0`, which the release workflow compares
  against the tag name and fails closed on mismatch.
- `uv run kanbanlan --version` reports `kanbanlan 0.7.0`, confirming the
  `__init__.py` bump agrees with the package metadata.
- `uv run pytest` — 152 passed, 12 subtests passed.
- `uv run ruff check .` — all checks passed.
- `uv run ruff format --check .` — 54 files already formatted.
- `uv build` — built `kanbanlan-0.7.0.tar.gz` and `kanbanlan-0.7.0-py3-none-any.whl`.

## Delivered result

Version bumped to 0.7.0 in `pyproject.toml`, `src/kanbanlan/__init__.py`, and
`uv.lock`. `SECURITY.md` now names the `0.7.x` support line.

The release carries two requests delivered to `main`:

- KBL-QTQV5A4FXZFQTJ3R7JFW5XSKMA (#28) — CLI error hints are rendered with
  `shlex.join`, so a printed command is safe to paste when an argument contains
  a space.
- KBL-N3ZOODQUYJBJ3N4GMIWP65AGA4 (#27, PR #29) — opt-in retry with bounded
  backoff for transient GitHub gateway failures on read commands, plus hints
  that name the outage possibility for transient failures and for `gh`'s
  `unknown owner type`.

Publishing the GitHub Release for tag `v0.7.0` triggers
`.github/workflows/release.yaml`, which builds and publishes to PyPI through
trusted publishing. That step cannot be undone or replaced for a given version.
