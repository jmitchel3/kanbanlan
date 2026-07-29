# Release Kanbanlan 0.5.0

- Kanbanlan: `KBL-XJGHABSWHVCCRBZLLXZBK5YPWA`
- Canonical home: `github`
- Canonical request: [#18](https://github.com/jmitchel3/kanbanlan/issues/18)

## Request

## Outcome

Kanbanlan 0.5.0 is published from the merged native agent session tracking feature and verified across GitHub and PyPI.

## Acceptance criteria

- [x] Package, lockfile, and CLI versions agree on 0.5.0.
- [x] Release-facing documentation reflects the 0.5.x line and native agent session tracking.
- [x] Tests, lint, formatting, build, artifact inspection, YAML validation, and credential scan pass.
- [ ] The release commit passes required CI on main.
- [ ] Annotated tag v0.5.0 and the GitHub Release target the release commit.
- [ ] The release workflow publishes immutable 0.5.0 artifacts to PyPI.
- [ ] A clean uvx invocation reports kanbanlan 0.5.0.

## Scope boundaries

Release preparation and publication only; no additional product behavior.

## Decisions

- Select `0.5.0` as a minor release because native agent session tracking adds
  substantial backward-compatible functionality after `0.4.1`.
- Keep tracking opt-in and preserve the merged implementation without adding
  unrelated product changes during release preparation.
- Update `SECURITY.md` so the supported line follows the newly published
  `0.5.x` series.
- Publish only through the existing GitHub `pypi` environment and OIDC Trusted
  Publishing workflow; no package token is introduced.

## Verification

- Git tag, GitHub Release, package metadata, and PyPI all reported `0.4.1`
  before preparation; no `0.5.0` artifact had escaped the repository.
- PR #17 and its post-merge `main` CI run passed on Python 3.11 through 3.14.
- `uv lock --check` resolved the 8-package lock without changes.
- `uv run pytest` passed all 131 tests; `uv run ruff check .`, `uv run ruff
  format --check .`, and `git diff --check` passed.
- `uv version --short`, `uv run kanbanlan --version`, `pyproject.toml`,
  `src/kanbanlan/__init__.py`, and `uv.lock` all report `0.5.0`.
- `uv build` produced `kanbanlan-0.5.0.tar.gz` and
  `kanbanlan-0.5.0-py3-none-any.whl`. Both archives report version `0.5.0`;
  their file lists and the wheel runtime version were inspected.
- Ruby Psych parsed all seven workflow, Dependabot, and issue-form YAML files.
- High-confidence GitHub token, PyPI token, AWS access-key, and private-key
  scans found no matches in tracked or untracked release inputs. The user-owned
  `docs/improvements/` files in the primary checkout remain untracked and excluded.
- The GitHub `pypi` environment exists, and `.github/workflows/release.yaml`
  grants `id-token: write` only to the environment-bound publish job.

## Delivered result

Kanbanlan is prepared as version `0.5.0` with opt-in native agent session
tracking for Codex, Claude Code, Grok Build, Google Antigravity AGY, and custom
attribution integrations. Package metadata and supported-version documentation
are aligned, immutable artifacts pass the complete local release gate, and the
repository remains configured for secretless publication through GitHub OIDC.
