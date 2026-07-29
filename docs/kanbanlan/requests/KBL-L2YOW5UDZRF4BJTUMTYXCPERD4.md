# Release Kanbanlan 0.6.0

- Kanbanlan: `KBL-L2YOW5UDZRF4BJTUMTYXCPERD4`
- Canonical home: `github`
- Canonical request: [#22](https://github.com/jmitchel3/kanbanlan/issues/22)

## Request

Publish the completed in-place `kanbanlan init` configuration update as the
next Kanbanlan release.

## Outcome

Kanbanlan 0.6.0 is available from GitHub and PyPI with existing-project-safe
`init` behavior and explicit session-tracking enable/disable controls.

## Acceptance criteria

- [x] Version declarations and lock metadata agree on 0.6.0.
- [x] The full local release gate passes and distribution metadata is correct.
- [ ] Required CI passes on the release commit before the tag is created.
- [ ] GitHub Release `v0.6.0` publishes successfully through trusted PyPI OIDC.
- [ ] PyPI and a clean `uvx` invocation report Kanbanlan 0.6.0.

## Scope boundaries

Release preparation and publication only; no additional product behavior.

## Decisions

- Use a minor bump from 0.5.0 because updating an existing configuration in
  place is meaningful backward-compatible functionality.
- Update `SECURITY.md` so the supported line follows the newly published
  `0.6.x` series.
- Preserve PyPI's immutable history and publish only after `main` CI succeeds.
- Continue using the GitHub `pypi` environment and secretless trusted
  publishing configured in `.github/workflows/release.yaml`.

## Verification

- Git tag, GitHub Release, package metadata, and PyPI all reported `0.5.0`
  before preparation; no `0.6.0` artifact had escaped the repository.
- PR #21 passed CI on Python 3.11 through 3.14 and merged as commit `9e00eef`.
- `uv lock --check` resolved the 8-package lock without changes.
- `uv run pytest` passed all 138 tests; `uv run ruff check .`, `uv run ruff
  format --check .`, and `git diff --check` passed.
- `pyproject.toml`, `src/kanbanlan/__init__.py`, `uv.lock`, and the CLI all
  report `0.6.0`.
- `uv build` produced `kanbanlan-0.6.0.tar.gz` and
  `kanbanlan-0.6.0-py3-none-any.whl`; wheel metadata and archive contents were
  inspected and report version `0.6.0`.
- Ruby Psych parsed all seven workflow, Dependabot, and issue-form YAML files.
- High-confidence GitHub token, PyPI token, AWS access-key, and private-key
  scans found no matches in tracked or untracked release inputs. The
  user-owned `docs/improvements/` files in the primary checkout remain
  untracked and excluded.
- The GitHub `pypi` environment exists, and `.github/workflows/release.yaml`
  grants `id-token: write` only to the environment-bound publish job.

## Delivered result

Kanbanlan is prepared as version `0.6.0` with safe in-place updates for existing
configuration, including explicit session-tracking enable and disable controls.
Package metadata and the supported-version policy are aligned, immutable
artifacts pass the complete local release gate, and publication remains
secretless through GitHub OIDC.
