# Kanbanlan

[![CI](https://github.com/jmitchel3/kanbanlan/actions/workflows/ci.yml/badge.svg)](https://github.com/jmitchel3/kanbanlan/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/jmitchel3/kanbanlan/blob/main/LICENSE)

Kanbanlan gives a repository one documented coordination workflow for humans
and coding agents. Today its canonical kanban home is GitHub Issues with a
GitHub Projects v2 projection; its core identity and provider contract are
portable to other canonical homes.

It provides:

- one-command repository and Project setup;
- browser-based GitHub CLI authentication when credentials or the `project`
  scope are missing;
- repository labels, issue/PR templates, and managed `AGENTS.md` /
  `CLAUDE.md` instructions;
- a private local snapshot shared across Git worktrees;
- dry-run-first reconciliation between issue labels, Project Status, active
  claims, and linked pull requests; and
- immutable, provider-independent Kanbanlan IDs;
- safe capture, claim, release, handoff, review, and close commands; and
- durable per-request records stored in the repository.

GitHub Issues remain canonical in the current provider. Kanbanlan does not
store API tokens, require an MCP server, or create another server-side database.

## Install

Kanbanlan requires Python 3.11+, Git, and
[GitHub CLI](https://cli.github.com/). The examples below use
[uv](https://docs.astral.sh/uv/) for installation and development.

Install the latest release from PyPI:

```sh
uv tool install kanbanlan
```

Once installed, upgrade to the latest release with:

```sh
kanbanlan upgrade
```

On normal CLI use, Kanbanlan also checks PyPI at most once every three days and
prints a short notice when a newer release is available. Set
`KANBANLAN_NO_UPDATE_CHECK=1` to disable these checks.

Or install the unreleased development version from GitHub:

```sh
uv tool install git+https://github.com/jmitchel3/kanbanlan.git
```

To install a local checkout instead:

```sh
uv tool install .
```

For development:

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv build
```

## Initialize a repository

Start the guided setup wizard from any GitHub-backed repository:

```sh
cd /path/to/repository
kanbanlan init
```

The three-step wizard detects the repository and default branch, defaults to a
fresh copy of the [Kanbanlan Project
template](https://github.com/users/jmitchel3/projects/6/views/5), collects the
staging and optional production branch, then shows a summary for confirmation
before it changes repository files or Project settings. The copied Project is
titled `<repository name> Delivery` by default and includes the template's
preconfigured views. GitHub and cache work shows progress as it runs, including
a clear failed step if setup stops.

If `.kanbanlan.toml` already exists, `init` switches to a local update-in-place
path before repository discovery or GitHub authentication. It reuses the stored
repository and Project binding, preserves unspecified settings, and refreshes
managed files without creating, copying, linking, or reconciling a Project.

```sh
kanbanlan init --session-tracking       # enable and scaffold hooks in place
kanbanlan init --no-session-tracking    # disable attribution in place
kanbanlan init                          # refresh managed files with stored settings
```

Disabling tracking leaves installed hook files in place but inert, which avoids
deleting custom hook configuration. Use `--force` only when existing custom
generated targets should be replaced. Repository- and Project-binding options
are rejected on the update path; pass `--reconfigure` to intentionally rerun
the complete setup wizard and choose or create a different Project.

You can also provide any choice up front. Reuse an existing Project:

```sh
cd /path/to/repository
kanbanlan init --project-url https://github.com/orgs/acme/projects/2
```

Create a new empty Project instead of using the default template:

```sh
kanbanlan init --create-project --project-title "Product Delivery" --open
```

Copy a different Project template, including its useful views:

```sh
kanbanlan init --template-project template-owner/1 --project-title "Product Delivery"
```

On the new-setup path, `init` authenticates through
`gh auth login --web` when necessary, ensures the GitHub token has the
`project` scope, links the Project to the repository, repairs the Status field,
creates the workflow labels, writes managed repository files, adds open issues,
and reconciles their state.

Use `--non-interactive` in automation; for a new setup without a Project source
it copies the default template. Provide `--project-number`, `--project-url`,
`--create-project`, or `--template-project` to override that default. Use
`--local-only` with an existing Project reference to generate repository files
without GitHub mutations. Pass `--no-open` to suppress the wizard's browser
question or `--open` to open the configured Project after setup.

Terminal colors distinguish headings, workflow states, priorities, warnings,
and errors when output is interactive. Use `--color always` or `--color never`
to choose explicitly. Kanbanlan also respects the standard `NO_COLOR`
environment variable. Progress is written to stderr so commands such as
`snapshot`, `path`, and `capture` keep clean, pipe-friendly stdout.

Kanbanlan does not create custom Project views or GitHub's built-in auto-add
workflow through the API. On a previously unconfigured repository, plain
`kanbanlan init` copies the default template so its views are present from the
start. With `--create-project`, `--open` opens the empty Project so a Board view
can be added manually. Kanbanlan's own `reconcile --apply` keeps item states
correct even when GitHub Project workflows are not configured.

### Optional background reconciliation

Successful live initialization and reconciliation register the repository with
one user-scoped worker. It deduplicates worktrees through Git's common
directory, refreshes enabled repositories on a bounded schedule, applies only
safe repairs, and records last-good snapshot and retry health without storing
credentials.

```sh
kanbanlan worker status
kanbanlan worker enable --github-login YOUR_GITHUB_ACCOUNT
kanbanlan worker start
kanbanlan worker stop
kanbanlan worker disable
```

The worker resolves the selected account's credential at runtime with
`gh auth token --user`; it never runs `gh auth switch` and never writes a token
to the registry. See [`docs/workflow/worker.md`](docs/workflow/worker.md) for
macOS LaunchAgent and Linux systemd user-service examples. The worker is
opt-in, has no Docker requirement, and explicit disablement persists.

## Daily use

```sh
kanbanlan ensure             # refresh only when the worktree-shared cache is stale
kanbanlan next               # report the first unblocked Ready issue
kanbanlan status             # summarize the local cache
kanbanlan reconcile          # report drift, without mutations
kanbanlan reconcile --apply  # apply and verify the displayed repairs
kanbanlan --json next        # stable output for agents and automation
```

The cache lives at `<primary-checkout>/.cache/kanbanlan/` with private file
permissions. A failed refresh preserves the last good snapshot and records the
error in `health.json`.

Refreshes respect the GitHub GraphQL point budget, which is shared by every
repository and agent using the same account. Concurrent sessions that queue on
a refresh reuse the fetch that just completed instead of repeating it. When
GitHub rate limits a refresh, `ensure` serves the last good snapshot and
records `refresh_status: "throttled"`; when the snapshot itself reports fewer
remaining points than `rate_limit_floor` in `.kanbanlan.toml` (default 500,
0 disables it), `ensure` defers refreshing until the quota resets. `status`
and `doctor` report the remaining points. An explicit `kanbanlan refresh`
always attempts the fetch and reports the rate limit plainly if it fails.

Refreshes are also incremental: a cheap probe reads only item identity and
`updatedAt` timestamps, and full item content (comments, labels, assignees) is
re-fetched only for cards that changed since the last refresh, using an
advisory raw-node cache in the same cache directory. Any doubt about the cache
falls back to a full fetch, so the cache can only reduce cost, never change
the snapshot. Set `KANBANLAN_FULL_REFRESH=1` to force the full fetch.

## Shared Projects across repositories

One GitHub Project can serve several repositories. Every command above reads at
repository scope, which keeps only content owned by the configured repository,
so a shared Project never hands this repository another repository's work.

Project scope is an explicit, read-only opt-in for the cross-repository overlap
check an agent owes before claiming work:

```sh
kanbanlan overlap                   # open cards and pull requests Project-wide
kanbanlan status --project          # board counts per repository
kanbanlan --json snapshot --project # project-scoped stable JSON
```

Project-scoped reads are live and never write the shared cache. They are
bounded to repositories the Project already references and never enumerate
repositories owned by the account. A peer repository that cannot be read is
reported in `source.unavailable_repositories` instead of failing the read.

Snapshots carry `source.scope`, and every issue and pull request carries
`repository` plus a repository-qualified `provider_ref` such as
`github:owner/repo#123`, so identically numbered content in different
repositories never collides. A bare issue number stays a local reference.

A request in one repository can be delivered by a pull request in another. The
pull request must say so explicitly, either with a repository-qualified closing
reference such as `Closes owner/repo#123` or by declaring the request's exact
Kanbanlan ID in its body. A bare `Closes #123` is only ever read inside the
pull request's own repository. `linked_open_pull_requests` reports the
`repository`, qualified `provider_ref`, and the `linked_by` routes that
justified each link, and `kanbanlan review` accepts a qualifying
cross-repository pull request while keeping the responsible claim.

Ambiguity is never guessed. A pull request naming several Kanbanlan IDs without
declaring one, or declaring an ID that more than one request carries, appears
in `linkage_problems` and links to nothing; `reconcile`, `overlap`, and
`review` surface it.

A request can also be captured into the repository that will implement it,
while staying on the shared Project:

```sh
kanbanlan capture "Add the HSA/FSA page" --repository OWNER/REPO
```

`capture` defaults to the configured repository and never infers a destination
from the title or body. The target is validated and prepared before anything is
created: it must be on the configured host, accessible, and linked to the
Project, and workflow labels are provisioned there. If a later step fails, the
error names the created URL and the repair to run, so a retry never creates a
second request.

A request that already exists moves with `rehome`, which preserves its
Kanbanlan ID, discussion, and Project state instead of fragmenting history into
a replacement:

```sh
kanbanlan rehome KBL-... --repository OWNER/REPO           # plan only
kanbanlan rehome KBL-... --repository OWNER/REPO --apply   # perform the move
```

The plan is the default and changes nothing; it reports what transfers, what
the target needs, what GitHub drops, and anything that blocks the move. A move
is refused while the request has an active claim or a linked open pull request,
when it is closed, or when the destination is where it already lives. It moves
the canonical request only: branches, worktrees, commits, and pull requests stay
where they are, and the request keeps its Kanbanlan ID while receiving a new
issue number.

## Request lifecycle

```sh
kanbanlan capture "Add export audit log" --priority priority:p1
kanbanlan triage KBL-...
kanbanlan claim KBL-... --touchpoints "audit API; exports UI; migrations"
kanbanlan record KBL-...
kanbanlan rehome KBL-... --repository other-owner/other-repo
kanbanlan review KBL-...
kanbanlan release KBL-... --reason "Waiting for product decision" --blocked
kanbanlan handoff KBL-... --session codex-next --branch work/kbl-audit \\
  --worktree /path/to/worktree --reason "Shift change"
kanbanlan close KBL-... --reason "Duplicate of KBL-..." --not-planned
```

`capture` assigns a globally unique `KBL-...` Kanbanlan ID. Lifecycle commands
accept that ID, a GitHub issue number, or the normalized GitHub provider
reference. `reconcile --apply` assigns IDs to requests created through GitHub's
web interface or by older Kanbanlan releases.

By default `claim` posts the claim first, verifies that it is the earliest
active claim, and only then creates a dedicated worktree from the configured
default branch. If checkout creation fails, it releases the claim and returns
the card to Ready. Use `--no-worktree` only from an existing non-default
branch/worktree.

A merged pull request closes its request and moves the card to Done. `close`
covers every other terminal outcome: work delivered without a pull request, a
duplicate, or a request that will not be built. It releases any active claim,
closes the canonical request as completed or, with `--not-planned`, as dropped,
and settles the projection at Done. It refuses while a linked pull request is
still open unless `--force` is given.

`record` creates `docs/kanbanlan/requests/<Kanbanlan ID>.md` once. Complete its
decisions, verification, and delivered-result sections in the implementation
PR. Kanbanlan never overwrites manual changes to an existing record. Volatile
status and claim movements remain in the live canonical home rather than Git.

### Optional agent session tracking

Provider-native session tracking is disabled by default. Enable it during fresh
setup or update an already configured repository in place with
`kanbanlan init --session-tracking`. The repository configuration is:

```toml
[session_tracking]
enabled = true
```

The repository setting can be overridden for one process with
`KANBANLAN_SESSION_TRACKING=true` or `false`. Session identity precedence is:

1. `--actor-session HARNESS:SESSION_ID`;
2. `KANBANLAN_AGENT_SESSION=HARNESS:SESSION_ID`;
3. `KANBANLAN_AGENT` together with `KANBANLAN_SESSION_ID`;
4. provider-native variables: `CODEX_THREAD_ID`, `CLAUDE_SESSION_ID`,
   `GROK_SESSION_ID`, or `AGY_CONVERSATION_ID`; and
5. unambiguous context registered by a generated agent lifecycle hook.

When none of these yields a trustworthy native ID, Kanbanlan records the
activity as unavailable instead of inventing a resumable session. A claim still
gets its ordinary coordination identifier so ownership remains backward
compatible.

```sh
kanbanlan sessions KBL-...                     # lifecycle sessions and resume commands
kanbanlan sessions KBL-... --action triage
kanbanlan resume KBL-...                       # print the latest resume command
kanbanlan resume KBL-... --action claim --run # resume through the native harness
```

Session entries keep the native ID and harness separate and display them as
`<session-id> · <harness>`. Resume adapters are included for Codex, Claude Code,
Grok Build, and Google Antigravity AGY. The generated hooks pass native session
context to `kanbanlan session-hook`; custom integrations can call that command
with their lifecycle JSON on stdin or set the Kanbanlan environment variables.
Other harness labels can still be attributed through explicit or environment
configuration, but need a resume adapter before `kanbanlan resume` can launch them.
Each event keeps its actor; claim and handoff events additionally distinguish
the session responsible for the resulting in-progress work. Resume uses that
responsible session, so a handoff resumes its recipient rather than its sender.

Enabling this feature writes native session IDs to canonical request comments.
Those IDs are not treated as credentials, but they can correlate local work and
may be visible publicly. Transcript paths are never published; hook context is
kept in a private local cache. Local sessions remain resumable only where the
original harness history and account are available.

## Portable architecture

The versioned configuration distinguishes the GitHub code host, the canonical
kanban home, and board projections. Normalized snapshots expose a Kanbanlan ID,
provider ID, display ID, repository-qualified provider reference, canonical URL,
lifecycle state, claims, and linked pull requests. Workflow reconciliation depends on a provider
contract; GitHub is its first implementation.

This keeps the CLI as the portable agent interface. MCP integrations may wrap
it, but agents can operate using ordinary shell access and `--json`. Linear or
Asana adapters, mirroring, webhooks, and multi-master conflict resolution are
deliberately outside the current implementation.

## Managed repository files

`init` writes:

- `.kanbanlan.toml`;
- `.github/ISSUE_TEMPLATE/work-request.yml`;
- `.github/pull_request_template.md`;
- `docs/workflow/kanbanlan.md`;
- a marked Kanbanlan section in `AGENTS.md` and `CLAUDE.md`; and
- `/.cache/kanbanlan/` and `/.worktrees/` in `.gitignore`.

When session tracking is enabled, `init` also creates non-destructive project
hooks for Codex, Claude Code, Grok Build, and AGY. Existing custom hook files are
skipped unless `--force` is explicitly passed.

Generated standalone files carry a marker. Existing custom templates are not
overwritten unless `--force` is passed. Agent instruction sections are
updated only between `kanbanlan:start` and `kanbanlan:end` markers.

## State model

| Issue label | Project Status |
| --- | --- |
| `status:intake` | Inbox |
| `status:ready` | Ready |
| `status:in-progress` | In progress |
| `status:blocked` | Blocked |
| `status:review` | In review |
| closed issue | Done |

Priorities are `priority:p0` through `priority:p3`. An active CLAIM forces In
progress; an open pull request that closes the issue forces In review; a closed
issue forces Done, whether a merge or `kanbanlan close` closed it. Issue labels are the fallback status record when the
Project is temporarily unavailable.

## Diagnostics

```sh
kanbanlan auth
kanbanlan doctor
kanbanlan path
kanbanlan snapshot
kanbanlan refresh
```

`doctor` checks configuration, authentication, Project Status options, labels,
and cache health without mutating GitHub.

## Contributing and security

Bug reports and focused pull requests are welcome. See
[CONTRIBUTING.md](https://github.com/jmitchel3/kanbanlan/blob/main/CONTRIBUTING.md)
for the development workflow. Please report security vulnerabilities privately
as described in
[SECURITY.md](https://github.com/jmitchel3/kanbanlan/blob/main/SECURITY.md).

Kanbanlan is available under the
[MIT License](https://github.com/jmitchel3/kanbanlan/blob/main/LICENSE).
