# init duplicates the managed block when the start marker is missing

- Kanbanlan: `KBL-TL32DIBXZJHA7NP3G5NS5ORGJA`
- Canonical home: `github`
- Canonical request: [#124](https://github.com/jmitchel3/kanbanlan/issues/124)
- Pull request: [#126](https://github.com/jmitchel3/kanbanlan/pull/126)

## Request

positiveform.com CLAUDE.md had a kanbanlan:end marker but no kanbanlan:start, so init --local-only kept the stale block and appended a second one. init should detect a lone end marker (or a block starting at the managed heading) and replace it, or refuse with a clear message, never duplicate.

## Decisions

- `_upsert_section` now classifies markers before writing. No markers appends a block; exactly one start before one end replaces it.
- A lone end marker is treated as the managed block when a line equal to the generated heading (`## Request Coordination Workflow`) sits above it. The last such heading before the marker is the block start, so earlier hand-written copies of the heading stay put.
- Every other shape (lone start, lone end with no heading above it, end before start, more than one start or end marker) leaves the file untouched and returns action `needs repair` with a detail message. `init` and the in-place update print a warning line for each such file. The command still exits 0 because the rest of setup (Project, labels, other files) succeeded; the warning names the file and the fix.
- AGENTS.md and CLAUDE.md are read and written as raw bytes, so CRLF line endings and other content outside the block survive byte-for-byte (universal-newline reads used to rewrite CRLF).

## Verification

- [x] `uv run pytest` (520 passed)
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv build`
- New `MalformedMarkerTests` in `tests/test_scaffold.py`: the positiveform.com shape (heading plus lone end marker, CRLF prefix, trailing content) is replaced once and is idempotent on rerun; last-heading selection; lone end without a heading, lone end with heading only below, lone start, end before start, two blocks, extra end marker are each left byte-identical with `needs repair`; a well-formed block preserves CRLF and a file without a trailing newline; init output prints the warning. Nine of these failed on main.

## Delivered result

`kanbanlan init` no longer duplicates the managed block when the start marker is missing. A stale block that begins at the generated heading is replaced in place, and any other malformed marker layout is reported as needing manual repair instead of being appended to.
