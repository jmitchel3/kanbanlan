# Generate the request record from the merged pull request

- Kanbanlan: `KBL-FI56WEGK3VDNBJZVUSH33INVEA`
- Canonical home: `github`
- Canonical request: [#104](https://github.com/jmitchel3/kanbanlan/issues/104)
- Pull request: [#108](https://github.com/jmitchel3/kanbanlan/pull/108)

## Request

The durable record in docs/kanbanlan/requests is generated from the PR body (decisions, verification, result) instead of hand-maintained duplicate prose.

## Decisions

- The pull request body is the single place an agent writes decisions, verification, and the delivered result. `kanbanlan record <ref> --from-pr` renders them into `docs/kanbanlan/requests/<Kanbanlan ID>.md` in the existing record format (title, metadata, Request from the issue body, Decisions, Verification, Delivered result), adding one `Pull request:` metadata line for traceability.
- Parsing and rendering live in `records.py` (`pull_request_sections`, `render_record_from_pull_request`, `write_record_from_pull_request`, `select_pull_request`, `current_branch`); `cli.py` only gains the flag and a small `_record_from_pull_request` hook.
- Sections are level-two headings matched case-insensitively. HTML comments (template guidance) are dropped, headings inside fenced code blocks are ignored, and a level-one heading ends a section. `## Result` maps to the record's `Delivered result`; `## Delivered result` is also accepted.
- The pull request is found from one live card read with `pull_requests=True` (no board read): the linked open pull request whose head is the current branch wins, otherwise the single linked one. None, or several with none on the branch, is an error rather than a guess.
- A missing or placeholder-only section is reported (stderr, and `missing_sections` in `--json`), left as a visible placeholder comment, and the command exits 1. Nothing is invented.
- `--from-pr` overwrites the record on every run (`created`, `updated`, or `unchanged`), because the PR body is the source of truth. Plain `kanbanlan record` keeps its create-once, never-overwrite behaviour.
- The scaffolded PR template and this repository's `.github/pull_request_template.md` now open with Request (Kanbanlan ID and Closes), Decisions, Verification, and Result sections. The workflow doc and README describe the `--from-pr` flow.

## Verification

- `uv run pytest`: 506 passed (19 new tests in `tests/test_records.py` covering section parsing, fences, comments, level-one headings, missing and placeholder sections, the `Delivered result` alias, the scaffolded template reporting all three sections missing, create/update/unchanged writes, replacing a plain record, pull request selection by branch and ambiguity, `current_branch` in and outside a git repo, and the CLI hook in text and JSON modes including the exit code).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `uv build`: wheel and sdist built.
- Dogfooded: this card's record was generated from this pull request body with `kanbanlan record #104 --from-pr`.

## Delivered result

`kanbanlan record <ref> --from-pr` generates or updates the durable request record from the linked pull request's Decisions, Verification, and Result sections, so agents write that content once, in the PR. Missing sections are reported with a non-zero exit. The PR templates carry the four sections. Default `kanbanlan record` behaviour is unchanged.
