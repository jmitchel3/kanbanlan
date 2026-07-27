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
- safe capture, claim, release, handoff, and review commands; and
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

The three-step wizard detects the repository and default branch, presents
existing Projects and create/copy actions as a numbered menu, collects the
staging and optional production branch, then shows a summary for confirmation
before it changes repository files or Project settings. GitHub and cache work
shows progress as it runs, including a clear failed step if setup stops.

You can also provide any choice up front. Reuse an existing Project:

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

`init` authenticates through
`gh auth login --web` when necessary, ensures the GitHub token has the
`project` scope, links the Project to the repository, repairs the Status field,
creates the workflow labels, writes managed repository files, adds open issues,
and reconciles their state.

Use `--non-interactive` in automation and provide `--project-number`,
`--project-url`, `--create-project`, or `--template-project` explicitly.
Use `--local-only` to generate repository files without GitHub mutations.
Pass `--no-open` to suppress the wizard's browser question or `--open` to open
the configured Project after setup.

Terminal colors distinguish headings, workflow states, priorities, warnings,
and errors when output is interactive. Use `--color always` or `--color never`
to choose explicitly. Kanbanlan also respects the standard `NO_COLOR`
environment variable. Progress is written to stderr so commands such as
`snapshot`, `path`, and `capture` keep clean, pipe-friendly stdout.

Kanbanlan does not yet create custom Project views or GitHub's built-in
auto-add workflow. Copying a template Project is the automated path when those
views matter. Without a template, `kanbanlan init --open` opens the Project so
a Board view can be added once. Kanbanlan's own `reconcile --apply` keeps item
states correct even when GitHub Project workflows are not configured.

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

## Request lifecycle

```sh
kanbanlan capture "Add export audit log" --priority priority:p1
kanbanlan claim KBL-... --touchpoints "audit API; exports UI; migrations"
kanbanlan record KBL-...
kanbanlan review KBL-...
kanbanlan release KBL-... --reason "Waiting for product decision" --blocked
kanbanlan handoff KBL-... --session codex-next --branch work/kbl-audit \\
  --worktree /path/to/worktree --reason "Shift change"
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

`record` creates `docs/kanbanlan/requests/<Kanbanlan ID>.md` once. Complete its
decisions, verification, and delivered-result sections in the implementation
PR. Kanbanlan never overwrites manual changes to an existing record. Volatile
status and claim movements remain in the live canonical home rather than Git.

## Portable architecture

The versioned configuration distinguishes the GitHub code host, the canonical
kanban home, and board projections. Normalized snapshots expose a Kanbanlan ID,
provider ID, display ID, provider reference, canonical URL, lifecycle state,
claims, and linked pull requests. Workflow reconciliation depends on a provider
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

## Contributing and security

Bug reports and focused pull requests are welcome. See
[CONTRIBUTING.md](https://github.com/jmitchel3/kanbanlan/blob/main/CONTRIBUTING.md)
for the development workflow. Please report security vulnerabilities privately
as described in
[SECURITY.md](https://github.com/jmitchel3/kanbanlan/blob/main/SECURITY.md).

Kanbanlan is available under the
[MIT License](https://github.com/jmitchel3/kanbanlan/blob/main/LICENSE).
