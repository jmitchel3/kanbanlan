# Release Kanbanlan 0.13.0

- Kanbanlan: `KBL-JO3KWEZPPZFIVEX3OGSX4ZV5DE`
- Canonical home: `github`
- Canonical request: [#121](https://github.com/jmitchel3/kanbanlan/issues/121)

## Request

Prepare and publish Kanbanlan 0.13.0: start/done/drop verbs with internals
under `admin` (#111), REST queue reads with ETag conditional requests (#110),
records generated from the pull request body (#108), shorter managed agent
instructions (#112), removal of the background worker and write-behind outbox
(#114), the `cli` package split (#115), live claim checks and no dropped cards
(#119), and faster `start` (#120).

## Decisions

- Select 0.13.0, a minor release with breaking changes: `sync` is removed,
  `worker` accepts only `uninstall`, and `KANBANLAN_WRITE_BEHIND` and
  `KANBANLAN_BACKGROUND_REFRESH` are gone. Lifecycle commands write GitHub
  synchronously.
- Keep the existing OIDC publishing workflow; no workflow or credential
  changes.
- Publish the annotated `v0.13.0` tag only after CI passes on the release
  commit on main. GitHub Release publication triggers PyPI publishing.
- `start` measures about 5 seconds, not the 2 second target; the remaining
  cost is the 1 second confirm read that guards against concurrent claims,
  which is kept.

## Verification

- `uv lock`: version updated; `uv run kanbanlan --version`: `kanbanlan 0.13.0`.
- `uv run pytest`: 510 passed (98 subtests).
- `uv run ruff check .` and `uv run ruff format --check .`: passed.
- `uv build`: sdist and wheel built.

## Delivered result

The 0.13.0 release candidate is prepared with matching package, source, and
lockfile versions and all local gates passed. After merge, publication uses
the existing release workflow; the installed CLI is upgraded and the 0.12
worker is uninstalled after PyPI verification.
