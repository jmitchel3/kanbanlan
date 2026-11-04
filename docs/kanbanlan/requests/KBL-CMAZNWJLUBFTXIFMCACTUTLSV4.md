# Split cli.py by command domain

- Kanbanlan: `KBL-CMAZNWJLUBFTXIFMCACTUTLSV4`
- Canonical home: `github`
- Canonical request: [#107](https://github.com/jmitchel3/kanbanlan/issues/107)

## Request

After the surface shrinks, cli.py is split into per-domain modules with no behaviour change.

## Decisions

- `kanbanlan.cli` is now a package. `__init__` exports only `main`, so the
  console script `kanbanlan = "kanbanlan.cli:main"` and `kanbanlan/__main__.py`
  are unchanged; `cli/__main__.py` keeps `python -m kanbanlan.cli` working.
- Modules by domain: `parser` (argument parser and command lists), `app`
  (entry point, legacy outbox replay, error reporting), `dispatch` (command
  lookup), `output` (JSON, result, error, and rate-limit text helpers),
  `context` (repository, provider, and agent-session context), `cards`
  (single-card reads and writes), `setup` (init, auth, upgrade, doctor,
  account, worker removal), `queue` (ensure, refresh, status, snapshot, path,
  next, overlap, reconcile), `cleanup`, `lifecycle` (capture, triage, claim,
  release, rehome, review, close, handoff), `sessions` (sessions, resume,
  session-hook), `records` (record), and `verbs` (start, done, drop).
- The old `_handler` looked commands up in `globals()`. `dispatch._handler`
  now looks them up on each command module at call time, importing those
  modules inside the function because the verbs run other commands through it.
- Modules share helpers by importing them, so a test that replaces a helper
  must reach every importing module. `tests/cli_patch.py` provides
  `patch_cli(name, ...)`, which patches the name in each `kanbanlan.cli`
  module that has it with one shared mock; tests that used
  `mock.patch("kanbanlan.cli.X")` or `mock.patch.object(cli, "X")` were
  rewritten to it mechanically, and direct imports now name the submodule.
- `_fetch_is_stale` had no caller after the earlier cards and was deleted,
  along with the test patches that only stubbed it.

## Verification

- `uv run pytest`: 479 passed, 98 subtests passed (same suite, no test removed).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `uv build`: the wheel ships `kanbanlan/cli/*.py`; `kanbanlan --help`,
  `python -m kanbanlan --version`, and `python -m kanbanlan.cli admin --help`
  run.
- Help text for the top level, `admin`, and every command (62 renderings)
  compared byte for byte against the `cli.py` on `origin/main`: identical.

## Delivered result

`src/kanbanlan/cli.py` (3578 lines) became 13 modules under
`src/kanbanlan/cli/`. The largest is `setup.py` at 767 lines, then
`lifecycle.py` at 730 and `verbs.py` at 516. No behaviour or output changed.
