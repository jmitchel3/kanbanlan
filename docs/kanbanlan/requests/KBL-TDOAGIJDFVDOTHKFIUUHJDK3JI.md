# Update existing init configuration in place

- Kanbanlan: `KBL-TDOAGIJDFVDOTHKFIUUHJDK3JI`
- Canonical home: `github`
- Canonical request: [#20](https://github.com/jmitchel3/kanbanlan/issues/20)

## Request

## Outcome

Running kanbanlan init against an already configured repository can make
explicit configuration changes, such as enabling or disabling native session
tracking, without creating, copying, or relinking a GitHub Project.

## Acceptance criteria

- [x] Existing configuration is detected before the new-project setup flow.
- [x] kanbanlan init --session-tracking preserves all existing bindings and
  enables tracking in place.
- [x] A reversible option disables tracking in place without recreating Project state.
- [x] An existing repository with no explicit update intent receives clear,
  non-destructive guidance.
- [x] Existing custom generated targets remain protected unless force is explicitly supplied.
- [x] Fresh-repository init behavior remains unchanged.
- [x] Tests and documentation cover both setup and update paths.

## Scope boundaries

No general interactive configuration editor or unrelated init redesign.

## Decisions

- Treat the presence of `.kanbanlan.toml` as an existing installation before
  repository discovery, GitHub authentication, or any Project operation.
- Make ordinary existing-repository `init` a local scaffold refresh that
  preserves all unspecified configuration. Session tracking uses a tri-state
  CLI option so omission preserves the stored value while `--session-tracking`
  and `--no-session-tracking` are explicit updates.
- Permit local branch, hostname, snapshot-freshness, and tracking overrides on
  the update path, but reject repository or Project-binding flags unless the
  user explicitly selects `--reconfigure`.
- Keep hook files installed when tracking is disabled. They become inert because
  `session-hook` checks the setting, and custom hook files are never deleted.
- Preserve the existing full setup behavior for unconfigured repositories and
  for intentional `init --reconfigure` runs.

## Verification

- `uv run pytest -q` — 138 tests and 8 subtests passed.
- `uv run ruff check .` and `uv run ruff format --check .` passed.
- `uv build` produced the source distribution and wheel successfully.
- `git diff --check` passed.
- Focused CLI, configuration, and scaffold coverage verifies enable, disable,
  no-op refresh, cancellation, binding-option rejection, full reconfiguration,
  custom-hook preservation, and new-repository behavior.

## Delivered result

`kanbanlan init` now distinguishes an existing installation from a new setup.
Existing repositories update locally and reuse their Project binding; session
tracking can be enabled or disabled without GitHub Project mutations. Full setup
is still available through the explicit `--reconfigure` escape hatch.
