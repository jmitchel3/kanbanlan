# Upgrade actions/download-artifact to v8

- Kanbanlan: `KBL-U5OW5K5KCFDD5PL5NFISB77QAA`
- Canonical home: `github`
- Canonical request: [#7](https://github.com/jmitchel3/kanbanlan/issues/7)

## Request

## Outcome

Upgrade the release workflow from actions/download-artifact v7 to v8 while preserving the existing distribution handoff from the build job to the PyPI publish job.

## Acceptance criteria

- [x] The publish job downloads the distribution artifact into dist/ with actions/download-artifact v8.
- [x] The v8 integrity and decompression behavior remains compatible with the actions/upload-artifact v7 producer.
- [x] Repository CI remains green and the maintenance delivery is documented.

## Decisions

- Keep the existing archived artifact contract: the build job uses
  `actions/upload-artifact@v7` and the publish job downloads and expands that
  named artifact into `dist/`.
- Accept v8's stricter digest-mismatch behavior. A mismatched distribution must
  stop publication instead of producing only a warning.
- Keep the focused one-line upgrade rather than changing the upload action in
  the same request.

## Verification

- The pull request's Python 3.11, 3.12, 3.13, and 3.14 CI jobs passed.
- `uv run pytest -q` — 70 tests and 4 subtests passed after updating the
  branch to current `main`.
- `uv run ruff check .` — passed.
- `uv run ruff format --check .` — all 36 files formatted.
- `git diff --check` — passed.
- Reviewed the upstream v8 contract: archived artifacts from
  `actions/upload-artifact` remain downloadable and decompressed by default;
  digest mismatches now fail closed.
- The release-only artifact handoff is not executed by pull-request CI, so the
  compatibility conclusion is based on the upstream action contract plus the
  unchanged named-artifact and destination-path configuration.

## Delivered result

The PyPI publish job now downloads `python-package-distributions` with
`actions/download-artifact@v8` into `dist/`. The build-side uploader and
the rest of the release workflow are unchanged.
