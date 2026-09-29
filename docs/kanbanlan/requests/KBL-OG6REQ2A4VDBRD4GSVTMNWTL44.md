# Release Kanbanlan 0.11.3

- Kanbanlan: `KBL-OG6REQ2A4VDBRD4GSVTMNWTL44`
- Canonical home: `github`
- Canonical request: [#90](https://github.com/jmitchel3/kanbanlan/issues/90)

## Request

Prepare and publish Kanbanlan 0.11.3 with the targeted capture fix (#89, closes #85). Verify the test, lint, formatting, build, and version gates; merge release preparation; publish the annotated tag and GitHub Release through OIDC Trusted Publishing; verify PyPI and a clean uvx invocation.

## Decisions

- Select 0.11.3 as a patch release containing #89 (capture places the new
  card with targeted writes instead of full-board reads; closes #85).
- Keep the existing secretless OIDC publishing workflow and `pypi`
  environment; no workflow or credential changes.
- Publish the annotated `v0.11.3` tag only after CI passes on the release
  commit on main. GitHub Release publication triggers PyPI publishing.

## Verification

- `uv lock --check`: passed.
- `uv run pytest`: 427 passed.
- `uv run ruff check .` and `uv run ruff format --check .`: passed.
- `uv run kanbanlan --version`: `kanbanlan 0.11.3`.
- `uv build`: sdist and wheel built; metadata in both, `pyproject.toml`,
  `src/kanbanlan/__init__.py`, and `uv.lock` agree on 0.11.3.
- Parsed all seven workflow, issue-form, and Dependabot YAML files; release
  jobs carry `timeout-minutes`.
- Publication and a clean `uvx --from kanbanlan==0.11.3 kanbanlan --version`
  check are performed after the release-preparation PR merges.

## Delivered result

The 0.11.3 release candidate is prepared with matching package, source, and
lockfile versions and all local gates passed. After merge, publication uses
the existing release workflow; the installed CLI and worker are upgraded and
restarted after PyPI verification so the local worker loads the fix.
