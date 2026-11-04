# Shrink managed agent instructions to the four verbs

- Kanbanlan: `KBL-FXCX3ZIC2JAJ3HGAH73ADEBHOE`
- Canonical home: `github`
- Canonical request: [#105](https://github.com/jmitchel3/kanbanlan/issues/105)
- Pull request: [#112](https://github.com/jmitchel3/kanbanlan/pull/112)

## Request

The generated AGENTS.md/CLAUDE.md block and docs/workflow/kanbanlan.md tell agents: run kanbanlan start before coding, stop on conflict, done when the PR is up. No ensure/reconcile/overlap ceremony.

## Decisions

- The managed AGENTS.md / CLAUDE.md block is now about ten lines: `kanbanlan start <ref-or-title> --touchpoints ...` before coding and work in the printed worktree; stop and ask on a refusal or a semantically conflicting claim; one card per session; decisions and verification go in the PR body; `kanbanlan done` to open the PR; `kanbanlan drop` to abandon; status questions are read-only; "remember this" means `kanbanlan admin capture`. The ensure/reconcile/overlap ritual is gone. A one-line session hook reminder appears only when session tracking is enabled.
- The generated docs/workflow/kanbanlan.md leads with the four verbs, then pull requests and records, states, and a compact admin reference. Internals agents never need (cache, worker, repository vs Project scope tables, cleanup heuristics, session-tracking env precedence) were removed; the README still documents them.
- Templates stay in scaffold.py (workflow.py holds no rendering code). The checked-in files were regenerated with `scaffold_repository` locally, with no network, and the unrelated `.kanbanlan.toml` rewrite was reverted.
- New tests/test_managed_files.py asserts this repository's AGENTS.md, CLAUDE.md, and docs/workflow/kanbanlan.md match the templates, and pins the instruction content and length.

## Verification

- `uv run pytest` (562 passed, including tests/test_managed_files.py)
- `uv run ruff check .`
- `uv run ruff format --check .`

## Delivered result

Agents now read a short instruction block centred on `start`, `done`, `drop`, and `status`, and the README daily-use section matches it. A drift test keeps the checked-in generated files in step with the templates.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
