# Kanbanlan

Kanbanlan turns a GitHub repository and a GitHub Projects v2 board into one
coordinated request workflow for humans and coding agents.

It provides:

- one-command repository and Project setup;
- browser-based GitHub CLI authentication when credentials or the `project`
  scope are missing;
- repository labels, issue/PR templates, and managed `AGENTS.md` /
  `CLAUDE.md` instructions;
- a private local snapshot shared across Git worktrees;
- dry-run-first reconciliation between issue labels, Project Status, active
  claims, and linked pull requests; and
- safe capture, claim, release, handoff, and review commands.

GitHub Issues remain canonical. Kanbanlan does not store API tokens and does
not create another server-side database.

## Install

Kanbanlan requires Python 3.11+, Git, and
[GitHub CLI](https://cli.github.com/).

Install the local checkout with uv:

```sh
uv tool install /Users/jmitch/devpkgs/kanbanlan
```

For development:

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv build
```

## Initialize a repository

Reuse an existing Project:

```sh
cd /path/to/repository
kanbanlan init --project-url https://github.com/orgs/acme/projects/2
```

Create a new Project:

```sh
kanbanlan init --create-project --project-title "Product Delivery" --open
```

Copy a Project template, including its useful views:

```sh
kanbanlan init --template-project template-owner/1 --project-title "Product Delivery"
```

`init` detects the repository and default branch, authenticates through
`gh auth login --web` when necessary, ensures the GitHub token has the
`project` scope, links the Project to the repository, repairs the Status field,
creates the workflow labels, writes managed repository files, adds open issues,
and reconciles their state.

Use `--non-interactive` in automation and provide `--project-number`,
`--project-url`, `--create-project`, or `--template-project` explicitly.
Use `--local-only` to generate repository files without GitHub mutations.

GitHub's public API supports Project fields and items but not creating custom
views or the web-only auto-add workflow. Copying a template Project is the
fully automated path when those views matter. Without a template,
`kanbanlan init --open` opens the Project so a Board view can be added once.
Kanbanlan's own `reconcile --apply` keeps item states correct even when GitHub
Project workflows are not configured.

## Daily use

```sh
kanbanlan ensure             # refresh only when the worktree-shared cache is stale
kanbanlan next               # report the first unblocked Ready issue
kanbanlan status             # summarize the local cache
kanbanlan reconcile          # report drift, without mutations
kanbanlan reconcile --apply  # apply and verify the displayed repairs
```

The cache lives at `<primary-checkout>/.cache/kanbanlan/` with private file
permissions. A failed refresh preserves the last good snapshot and records the
error in `health.json`.

## Request lifecycle

```sh
kanbanlan capture "Add export audit log" --priority priority:p1
kanbanlan claim 123 --touchpoints "audit API; exports UI; migrations"
kanbanlan review 123
kanbanlan release 123 --reason "Waiting for product decision" --blocked
kanbanlan handoff 123 --session codex-next --branch work/123-audit \\
  --worktree /path/to/worktree --reason "Shift change"
```

By default `claim` posts the claim first, verifies that it is the earliest
active claim, and only then creates a dedicated worktree from the configured
default branch. If checkout creation fails, it releases the claim and returns
the card to Ready. Use `--no-worktree` only from an existing non-default
branch/worktree.

## Managed repository files

`init` writes:

- `.kanbanlan.toml`;
- `.github/ISSUE_TEMPLATE/work-request.yml`;
- `.github/pull_request_template.md`;
- `docs/workflow/kanbanlan.md`;
- a marked Kanbanlan section in `AGENTS.md` and `CLAUDE.md`; and
- `/.cache/kanbanlan/` in `.gitignore`.

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
issue forces Done. Issue labels are the fallback status record when the
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
