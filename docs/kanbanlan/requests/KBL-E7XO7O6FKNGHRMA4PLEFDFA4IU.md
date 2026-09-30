# Release Kanbanlan 0.12.0

- Kanbanlan: `KBL-E7XO7O6FKNGHRMA4PLEFDFA4IU`
- Canonical home: `github`
- Canonical request: [#98](https://github.com/jmitchel3/kanbanlan/issues/98)

## Request

Prepare and publish Kanbanlan 0.12.0: lifecycle commands never block (#97, card #92), rate-limited changes re-queue after reset (#93, card #84), writes proceed during refresh cooldown (#94, card #83), worker refreshes each repository and Project once per cycle and runs as a single instance on macOS (#95, card #86), and targeted capture (#89, card #85; prepared as 0.11.3 but never published). Verify gates, merge release preparation, publish the annotated tag and GitHub Release through OIDC Trusted Publishing, verify PyPI and a clean uvx invocation.

## Decisions

- Select 0.12.0, a minor release: lifecycle commands change behaviour
  (they decide locally and verify at sync time, and a stale local view can
  accept a change that then fails at sync), and the outbox gains a `waiting`
  state.
- Include #89 (targeted capture, card #85), #94 (writes during refresh
  cooldown, card #83), #93 (re-queue rate-limited changes, card #84), #95
  (worker dedupe and single instance on macOS, card #86), and #97 (lifecycle
  commands never block, card #92).
- 0.11.3 was prepared on main (#91) and built locally but never tagged or
  published; under the release policy it is not reused, and 0.12.0 supersedes
  it.
- Keep the existing secretless OIDC publishing workflow and `pypi`
  environment; no workflow or credential changes.
- Publish the annotated `v0.12.0` tag only after CI passes on the release
  commit on main. GitHub Release publication triggers PyPI publishing.

## Verification

- `uv lock --check`: passed.
- `uv run pytest`: 487 passed (77 subtests).
- `uv run ruff check .` and `uv run ruff format --check .`: passed.
- `uv run kanbanlan --version`: `kanbanlan 0.12.0`.
- `uv build`: sdist and wheel built; metadata in both, `pyproject.toml`,
  `src/kanbanlan/__init__.py`, and `uv.lock` agree on 0.12.0.
- Parsed all seven workflow, issue-form, and Dependabot YAML files.
- Running main (cd46c14) installed locally: capture, triage, and claim of
  this release card each returned immediately, and the restarted worker is the
  single instance holding `worker.lock`, with `worker status` reporting the
  duplicate rangertrac.org registration.
- Publication and a clean `uvx --from kanbanlan==0.12.0 kanbanlan --version`
  check are performed after the release-preparation PR merges.

## Delivered result

The 0.12.0 release candidate is prepared with matching package, source, and
lockfile versions and all local gates passed. After merge, publication uses
the existing release workflow; the installed CLI and worker are upgraded and
restarted after PyPI verification.

Follow-ups noted in the included records: `record` waits for a queued
capture to drain; back-to-back detached refreshes are not coalesced; `ensure`
can serve very old snapshots with a warning; no command removes a stale
worker registration.
