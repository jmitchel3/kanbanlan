# Release Kanbanlan 0.11.2

- Kanbanlan: `KBL-QFMW7GJBZJETPJNZGTUZM5JFTY`
- Canonical home: `github`
- Canonical request: [#80](https://github.com/jmitchel3/kanbanlan/issues/80)

## Request

Prepare and publish Kanbanlan 0.11.2 with the reconciliation rate-limit fix (#79) and the fixes already on main (#33 and #76). Verify the full test, lint, formatting, build, YAML, and version gates; merge release preparation; publish the annotated tag and GitHub Release through OIDC Trusted Publishing; verify PyPI and a clean uvx invocation. Version 0.11.1 has already produced local artifacts and will not be reused.

## Decisions

- Select 0.11.2 as a patch release. GitHub and PyPI last published 0.11.0;
  0.11.1 already produced local artifacts and cannot be reused under the
  release policy.
- Include #79 (reconciliation quota reserves and account cooldowns), #33
  (primary-checkout configuration fallback), and #76 (capture visibility and
  failed-sync diagnostics).
- Keep the existing secretless OIDC publishing workflow and `pypi` environment.
  Only the publish job has `id-token: write`.
- Preserve the user's two untracked improvement documents; neither enters
  the release worktree or distributions. No new credentials were introduced.
- Publish the annotated `v0.11.2` tag only after CI passes on the release
  commit on main. GitHub Release publication triggers PyPI publishing.

## Verification

- `uv lock --check`: passed.
- `uv run pytest`: 417 passed.
- `uv run ruff check .` and `uv run ruff format --check .`: passed.
- `uv run kanbanlan --version`: `kanbanlan 0.11.2`.
- `uv build`: source distribution and wheel built successfully. Metadata in
  both distributions, `pyproject.toml`, `src/kanbanlan/__init__.py`, and
  `uv.lock` agrees on 0.11.2.
- Parsed all seven GitHub workflow, issue-form, and Dependabot YAML files;
  verified release trigger and publishing permission structure.
- Scanned tracked and untracked files for credential patterns: no matches.
- Publication evidence is retained in the [GitHub Release](https://github.com/jmitchel3/kanbanlan/releases/tag/v0.11.2),
  its Actions run, and [PyPI version page](https://pypi.org/project/kanbanlan/0.11.2/).
  Publication and a clean `uvx --from kanbanlan==0.11.2 kanbanlan --version`
  smoke check are performed after the release-preparation PR merges.

## Delivered result

The 0.11.2 release candidate is prepared with matching package, source, and
lockfile versions and all local gates passed. Existing worker documentation
describes the included rate-limit fix. After merge, publication uses the
existing release workflow; the installed CLI and worker are upgraded and
restarted after PyPI verification so the local worker loads the fix.
