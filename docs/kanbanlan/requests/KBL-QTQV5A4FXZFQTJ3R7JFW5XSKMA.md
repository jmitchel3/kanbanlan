# Quote command arguments in CLI error hints

- Kanbanlan: `KBL-QTQV5A4FXZFQTJ3R7JFW5XSKMA`
- Canonical home: `github`
- Canonical request: [#26](https://github.com/jmitchel3/kanbanlan/issues/26)

## Request

## Outcome

When a `gh`/`git` command fails, the hint Kanbanlan prints is a runnable command that can be pasted into a shell verbatim.

## Problem

`_friendly_error` in `src/kanbanlan/cli.py` rebuilds the failed command with `" ".join(result.args)`. Any argument containing a space is emitted unquoted, so the printed hint is a different command than the one that ran.

Observed during `kanbanlan init` on another repository. The copy step failed and printed:

    Hint: Run `gh project copy 6 --source-owner jmitchel3 --target-owner paracord-clients --title prevenir-automations Delivery --format json` directly for more detail.

Pasting that produces a new and unrelated failure, `accepts at most 1 arg(s), received 2`, because the two-word title becomes two positional arguments. That sends the operator chasing a quoting bug that does not exist in Kanbanlan, which builds argv as a list.

`CommandError.__init__` in `src/kanbanlan/runner.py` already formats this correctly with `shlex.join`; only the CLI hint path is wrong.

## Acceptance

- Failed-command hints and the `command failed (...)` message use `shlex.join`.
- Regression test covers an argument containing a space.

## Decisions

Reused `shlex.join` rather than adding a formatting helper. `CommandError` in
`src/kanbanlan/runner.py` already renders failed commands that way, so both
paths now produce identical text and there is one convention to remember.

`_friendly_error` builds `command` once and uses it for both the hint and the
`command failed (...)` message, so the single-line change fixes both.

Left `result.args` comparisons in this function alone. They match on argv
elements (`result.args[0] == "gh"`, `result.args[:3] == (...)`) and never on
the joined string, so quoting does not affect hint selection.

## Verification

- `uv run pytest` — 139 passed, 8 subtests passed.
- `uv run ruff check .` — all checks passed.
- `uv run ruff format --check .` — 52 files already formatted.
- Confirmed the new test is a real regression test by reverting the one-line
  change and re-running it. Without the fix it fails, reporting the unquoted
  `--title prevenir-automations Delivery`; with the fix it passes.

## Delivered result

`_friendly_error` in `src/kanbanlan/cli.py` now renders the failed command with
`shlex.join(result.args)` instead of `" ".join(result.args)`. Hints are
therefore paste-safe when any argument contains a space, which is routine for
`--title` on `gh project create` and `gh project copy`.

Added `test_failed_command_is_reported_as_a_runnable_command` in
`tests/test_cli.py`, covering a `gh project copy` failure whose title contains
a space. It asserts the quoted form is present and the bare form is absent.

Behavior only affects diagnostic output; no command construction changed, since
argv was always passed as a list.

Follow-up, tracked separately as `KBL-N3ZOODQUYJBJ3N4GMIWP65AGA4` (#27): the
transient GitHub 5xx retry in `Runner.run`. That is the failure that surfaced
this hint in the first place.
