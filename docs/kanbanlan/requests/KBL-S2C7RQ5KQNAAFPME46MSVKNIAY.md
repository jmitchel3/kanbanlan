# Release Kanbanlan 0.4.1

- Kanbanlan: `KBL-S2C7RQ5KQNAAFPME46MSVKNIAY`
- Canonical home: `github`
- Canonical request: [#14](https://github.com/jmitchel3/kanbanlan/issues/14)

## Request

## Outcome

Kanbanlan 0.4.1 is published from the merged reliability fix and verified across GitHub and PyPI.

## Acceptance criteria

- [x] Package, lockfile, and CLI versions agree on 0.4.1.
- [x] Supported-version documentation reflects the 0.4.x line.
- [x] Tests, lint, formatting, build, artifact inspection, YAML validation, and credential scan pass.
- [ ] The release commit passes required CI on main.
- [ ] Annotated tag v0.4.1 and the GitHub Release target the release commit.
- [ ] The release workflow publishes immutable 0.4.1 artifacts to PyPI.
- [ ] A clean uvx invocation reports kanbanlan 0.4.1.

## Decisions

- Select `0.4.1` as a patch release because the complete delta from `v0.4.0` is a
  backward-compatible subprocess reliability fix, tests, and CI maintenance.
- Release from the merged `main` history; do not include the user-owned improvement
  review documents that remain untracked in the primary worktree.
- Update `SECURITY.md` as release-facing documentation so the supported line matches
  the published `0.4.x` series.
- Publish exclusively through the existing GitHub `pypi` environment and OIDC Trusted
  Publishing workflow; no package token is introduced or stored.

## Verification

- `uv lock --check` resolved the 8-package lock without changes.
- `uv run pytest` passed all 104 tests; `uv run ruff check .`, `uv run ruff
  format --check .`, and `git diff --check` passed.
- `uv run kanbanlan --version`, `pyproject.toml`, `src/kanbanlan/__init__.py`,
  and `uv.lock` all report `0.4.1`.
- `uv build` produced `kanbanlan-0.4.1.tar.gz` and
  `kanbanlan-0.4.1-py3-none-any.whl`. Both archives report `Version: 0.4.1`;
  their contents and the wheel's runtime `__version__` were inspected.
- Ruby Psych parsed all seven workflow, Dependabot, and issue-form YAML files.
- High-confidence GitHub token, PyPI token, AWS access-key, and private-key
  scans found no matches in tracked or untracked release inputs.
- The GitHub `pypi` environment exists, and `.github/workflows/release.yaml`
  grants `id-token: write` only to the environment-bound publish job.

## Delivered result

Kanbanlan is prepared as version `0.4.1` with the merged subprocess timeout and
CI hardening changes. Package metadata and supported-version documentation are
aligned, the immutable artifacts pass the complete local release gate, and the
repository remains configured for secretless publication through GitHub OIDC.
