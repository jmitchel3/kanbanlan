# Queued capture fails when GitHub lags, and failed changes lose their error message

- Kanbanlan: `KBL-NX7IVAKUM5FUZDJ7UI5ST4NYX4`
- Canonical home: `github`
- Canonical request: [#75](https://github.com/jmitchel3/kanbanlan/issues/75)

## Request

## Outcome

A queued capture survives GitHub listing a just-added Project item a moment late, and a failed queued change records the real error.

Observed: capture of #74 through the write-behind queue failed; the recorded error was the literal "}" because the drainer parsed only the last line of the child's pretty-printed JSON error. The issue had been created and added to the Project, but the capture's immediate re-read did not see it yet, so the capture failed and blocked the queued triage and claim.

## Acceptance criteria

- [x] The drainer extracts the message from a multi-line JSON error.
- [x] Capture retries its post-create read with a short bounded backoff before failing.
- [x] Tests cover both.

## Decisions

- `--json` errors are written as one indented JSON document to stderr,
  sometimes after progress lines, so `_executor_error` now decodes from
  every line that opens an object (newest first) with `raw_decode` rather
  than parsing single lines. The hint is kept beside the message.
- Capture reads the board up to five times (waiting 1, 2, 3, 4 seconds,
  about ten seconds in all) until the new request is listed before it
  reconciles. Creating the issue is not idempotent, so giving up
  immediately on a lagging read left a real issue behind a failed command,
  and in the queue that failure also blocked the request's triage and
  claim. After the last attempt it proceeds exactly as before, so a
  persistent problem still fails loudly. The peer-repository path gets the
  same wait.
- Observed on #74: the capture child created the issue and added it to the
  Project, but its immediate re-read did not list it; a refresh moments
  later did. The queue's rules held (triage and claim were not attempted),
  only the reported error was useless.

## Verification

- `uv run pytest -q`: 407 passed, 37 subtests; ruff check and format clean.
- New tests: an indented JSON error after progress output keeps its
  message and hint; a message containing braces and newlines is not split;
  capture waits until the request is listed (sleeps of 1s and 2s before
  the third read succeeds); the wait is bounded at five reads and ten
  seconds. The capture routing tests now give the mocked store a real
  snapshot, since the repository path reads it.

## Delivered result

A queued capture now rides out GitHub listing a new Project item a few
seconds late, and a failed queued change records the child command's real
error message and hint instead of the last line of its JSON.
