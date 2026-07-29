# Add opt-in native agent session tracking

- Kanbanlan: `KBL-DEILRNMOZZH5RPGQPS4AQW4JKY`
- Canonical home: `github`
- Canonical request: [#16](https://github.com/jmitchel3/kanbanlan/issues/16)

## Request

## Outcome

Kanbanlan can optionally attribute lifecycle events to resumable, provider-native AI agent sessions without changing default behavior.

## Acceptance criteria

- [x] Session tracking is disabled by default and enabled through repository configuration.
- [x] Enabled tracking recognizes supported provider-native session identifiers without inventing a random resumable identity.
- [x] Capture, triage, claim, handoff, release, and review activity preserve structured session attribution where available.
- [x] Existing repositories and claim parsing remain backward compatible.
- [x] Documentation explains configuration, supported agents, privacy/locality constraints, and fallback behavior.
- [x] Automated tests cover disabled, detected, explicitly supplied, and unavailable session contexts.

## Decisions

- Tracking is opt-in through `[session_tracking].enabled = true`; the
  `KANBANLAN_SESSION_TRACKING` process environment variable can override it.
- Native identity is represented as separate `harness` and `session_id` fields.
  Human output uses `<session-id> · <harness>` while claims use the unambiguous
  `harness:session-id` reference.
- Activity distinguishes the actor from the resulting responsible session. For
  claim and handoff, resume targets the owner of the resulting in-progress work.
- Lifecycle attribution is stored as a readable issue comment plus a versioned,
  machine-readable marker. No transcript path is published.
- Detection prefers an explicit CLI value, then Kanbanlan environment variables,
  native harness variables, and finally unambiguous private hook context. It
  fails closed when context is unavailable or ambiguous.
- Resume commands are allowlisted adapters rather than shell fragments supplied
  by comments. Codex, Claude Code, Grok Build, and AGY are supported initially.

## Verification

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run pytest -q` — 131 tests and 8 subtests passed.
- `uv run kanbanlan --help` and `uv run kanbanlan init --help`
- `uv build` — source distribution and wheel built successfully.

## Delivered result

Opt-in native session tracking now attributes each supported request lifecycle
event, exposes card history and safe native resume commands, scaffolds hooks for
the four initial harnesses, and supports repository, CLI, generic environment,
and provider-native environment configuration. Tracking remains disabled in
new repositories unless explicitly selected.
