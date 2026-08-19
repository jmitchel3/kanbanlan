# AGENTS.md

<!-- kanbanlan:start -->
## Request Coordination Workflow

The canonical kanban home is `github`. GitHub Issues currently
store canonical requests, and [jmitchel3 Project
2](https://github.com/users/jmitchel3/projects/2) is their projection. Repository policy
and durable delivery records remain versioned here. Follow
`docs/workflow/kanbanlan.md`.

- At session start run `kanbanlan ensure`. Before mutations run
  `kanbanlan reconcile` and inspect all open cards and pull requests for
  semantic overlap. When this repository shares its Project with other
  repositories, run `kanbanlan overlap` so the check covers them too. If live
  coordination state is unavailable, do not start potentially overlapping
  implementation.
- Status questions are read-only. “Remember this” creates an Inbox card.
  “Let's work on this” authorizes a live overlap check and one claim.
- Create or reuse one request per independently reviewable outcome. Each request
  has a provider-independent Kanbanlan ID. One session
  may own exactly one `status:in-progress` card, and one card may have exactly
  one active session. Claim with
  `kanbanlan claim <kanbanlan-id-or-provider-ref> --touchpoints ...`.
- Use a dedicated request branch and worktree. Block semantic conflicts even when
  filenames differ. Do not expand a claimed card into another useful outcome.
- Provider-native session tracking is enabled. Keep the generated agent hook
  active, and let lifecycle commands auto-detect the current agent session. Use
  `--actor-session HARNESS:SESSION_ID` only when automatic context is unavailable.
- Run `kanbanlan record <kanbanlan-id-or-provider-ref>` in the implementation
  worktree and complete its durable decisions, verification, and delivered result.
- A pull request closes its issue and moves it to In review. Ownership lasts
  until merge, explicit release, or handoff. Project Done means delivered to
  `main`; production readiness still requires staging review.
<!-- kanbanlan:end -->
