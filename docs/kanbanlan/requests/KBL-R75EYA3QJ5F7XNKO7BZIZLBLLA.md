# Bound subprocess and CI execution time

- Kanbanlan: `KBL-R75EYA3QJ5F7XNKO7BZIZLBLLA`
- Canonical home: `github`
- Canonical request: [#12](https://github.com/jmitchel3/kanbanlan/issues/12)

## Request

## Outcome

Kanbanlan subprocesses and CI jobs fail within explicit bounds instead of hanging
indefinitely.

## Acceptance criteria

- [x] Runner subprocesses default to a 60-second timeout.
- [x] Subprocess timeouts become `CommandError` instances with actionable command context.
- [x] Human-interactive commands explicitly opt out of the default timeout.
- [x] The CI test job and network-bound setup steps have explicit time limits.
- [x] Tests cover timeout propagation, opt-out, and timeout error conversion.

## Decisions

- Use a per-invocation 60-second default in `Runner.run`, so existing non-interactive
  `gh`, `git`, and `uv` calls inherit the bound without call-site churn.
- Convert `subprocess.TimeoutExpired` into the existing `CommandError` contract with
  synthetic exit code 124, preserving partial stdout/stderr and the elapsed limit.
- Require legitimate human-interactive calls to opt out explicitly with `timeout=None`.
- Bound the CI matrix job at 10 minutes and its network-bound setup steps at 5 minutes.

## Verification

- `uv run pytest` — 104 tests passed.
- `uv run ruff check .` — all checks passed.
- `uv run ruff format --check .` — 45 files already formatted.
- `ruby -e 'require "yaml"; YAML.parse_file(ARGV.fetch(0))' .github/workflows/ci.yml`
  — workflow is valid YAML.
- `git diff --check` — no whitespace errors.

## Delivered result

All non-interactive commands executed through `Runner` now inherit a 60-second limit.
Timeouts retain partial output and flow through the established `CommandError` and friendly
error handling paths. Interactive authentication, browser, and upgrade commands explicitly
remain unbounded. CI now caps the complete matrix job at 10 minutes and each network-bound
setup step at 5 minutes.
