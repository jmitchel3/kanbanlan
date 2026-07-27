---
name: release
description: Select, prepare, publish, and verify the next Kanbanlan release on GitHub and PyPI. Use when the user invokes /release, asks to release or publish Kanbanlan, requests a version bump, or wants the next package version shipped. Choose the version without asking the user unless a non-version product decision blocks the release.
---

# Release Kanbanlan

Release Kanbanlan end-to-end. Treat invoking this skill as authorization to
update versions, commit release-scoped changes, push `main` and the release tag,
create the GitHub Release, and publish through the `pypi` environment. Never ask
the user which version number to use.

## Choose the version

Inspect the latest Git tag, GitHub Release, PyPI release, current package
version, and changes since the latest release. Select a semantic version:

- Patch for backward-compatible fixes, documentation, tests, CI, packaging, or
  maintenance only.
- Minor for new functionality or meaningful backward-compatible behavior.
- Major for intentional breaking changes once the project is at least `1.0`.
  Before `1.0`, use a minor bump for breaking or substantial changes.

If there is no published release, use the prepared project version when it is
greater than any existing tag; otherwise choose the smallest suitable next
version. Never reuse a version already present on PyPI.

## Prepare

1. Read repository instructions and inspect the complete worktree, history,
   remote, tags, GitHub Releases, Actions state, and PyPI package state.
2. Preserve user changes. Review every file that will ship and scan tracked and
   untracked files for credentials or private data before staging.
3. Update `pyproject.toml` and `src/kanbanlan/__init__.py` to the chosen version.
   Refresh `uv.lock`, then confirm all three versions agree and the CLI reports
   the same value.
4. Update release-facing documentation when behavior, installation, supported
   versions, or release instructions changed.
5. Run the full gate:

   ```sh
   uv lock --check
   uv run pytest
   uv run ruff check .
   uv run ruff format --check .
   uv build
   ```

   Inspect the wheel metadata and verify the source distribution and wheel use
   the chosen version. Validate all GitHub workflow and issue-form YAML.

Do not proceed while checks fail or the worktree contains unexplained sensitive
material.

## Publish

1. Commit all reviewed release changes with a concise release-oriented commit
   message and push `main`, setting its upstream when needed.
2. Ensure the repository has the GitHub environment `pypi`. Keep PyPI
   authentication secretless: `.github/workflows/release.yaml` must use OIDC
   Trusted Publishing and grant `id-token: write` only to its publish job.
3. Wait for every required CI job on the pushed commit. If CI fails, diagnose,
   fix, commit, push, and wait again. Do not create the release tag before CI
   succeeds.
4. Create an annotated tag named `v<version>`, push it, and create a published
   GitHub Release from that exact tag with generated release notes.
5. Wait for the `release.yaml` run to finish. Do not report success based only
   on creating the GitHub Release.

Never force-push, move a published tag, delete release history, use
`skip-existing`, or store a PyPI token. PyPI distributions are immutable. If a
published tag or partial upload makes the selected version unusable, diagnose
the state and prepare a new patch version instead of rewriting history.

## Verify

Confirm all of the following before declaring success:

- GitHub `main` points at the intended release commit and is clean locally.
- The tag and GitHub Release exist and target that commit.
- CI and the release workflow completed successfully.
- PyPI reports the chosen version for `kanbanlan`.
- A clean temporary `uvx --from kanbanlan==<version> kanbanlan --version`
  invocation reports the same version.

Report the chosen version, commit, tag, GitHub Release URL, PyPI URL, workflow
results, and verification commands. If an external service is still processing,
keep monitoring rather than handing off an incomplete release.
