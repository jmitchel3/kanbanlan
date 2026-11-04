# AGENTS.md

<!-- kanbanlan:start -->
## Request Coordination Workflow

Requests live on [jmitchel3 Project 2](https://github.com/users/jmitchel3/projects/2).
Details: `docs/workflow/kanbanlan.md`.

- Before coding, run `kanbanlan start <ref-or-title> --touchpoints "..."` and
  work only in the worktree it prints.
- If `start` refuses, or lists another claim that conflicts semantically with
  this work, stop and ask.
- One card per session. Do not expand its scope into another outcome.
- Write decisions and verification in the pull request body.
- When the work is ready, run `kanbanlan done` (it opens or finds the pull
  request). `kanbanlan drop` abandons the card.
- Status questions are read-only. "Remember this" means `kanbanlan admin capture`.
- Keep the generated session hook active; commands detect this session.
<!-- kanbanlan:end -->
