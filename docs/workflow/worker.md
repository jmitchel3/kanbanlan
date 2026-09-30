# Background reconciliation worker

The worker is an opt-in user-level process. It services every enabled local
Kanbanlan repository once, keyed by each repository's Git common directory, so
linked worktrees do not create duplicate refresh loops.

## Lifecycle

```sh
kanbanlan worker status
kanbanlan worker enable --github-login YOUR_GITHUB_ACCOUNT
kanbanlan worker start
kanbanlan worker stop
kanbanlan worker disable
```

`init` and a successful live `reconcile` register a repository automatically.
Local-only setup, skipped reconciliation, failed setup, and unresolved drift do
not register it. `worker disable` writes an explicit tombstone; later setup
does not silently re-enable that repository.

The registry is stored under the user configuration directory with restrictive
permissions. It contains repository identity, account/host selection, status,
retry timestamps, and health metadata, but no token. Each run obtains the
selected account's token from the GitHub CLI and passes it only to subprocesses
through `GH_HOST` and `GH_TOKEN`.

Live reconciliation checks the cached GraphQL quota before starting either the
Project read or the open-issue read. Below `[local].rate_limit_floor` (500 by
default), it defers until GitHub's recorded reset time. It preserves the last
good snapshot for status reads and reports a throttled error; it never treats
an old snapshot as a successful live reconciliation.

When a worker job is throttled, other jobs using the same GitHub host and
account wait too, including after the worker restarts. Other accounts continue
normally. The worker honors current account bindings. A GitHub rate-limit
failure also records a cooldown in the repository cache, preventing repeated
`reconcile` and `refresh` commands from immediately retrying. If no valid reset
time is known, the cache uses a one-minute cooldown. Setting
`rate_limit_floor = 0` disables the proactive reserve; it does not bypass a
cooldown from GitHub refusing requests.

## One refresh per repository and Project

A repository registered from more than one clone (for example a live checkout
and a forgotten copy) is refreshed once per cycle. The worker services the
clone whose root is still a Git checkout and whose Git files (`HEAD`, `index`,
`FETCH_HEAD`, reflog) changed most recently, and skips the others.

Repository snapshots are repository-scoped: each refresh paginates the whole
Project but keeps only its own repository's content, so one repository's
refresh cannot stand in for another's. When several registered repositories
share one Project, the worker refreshes that Project at most once per cycle
and rotates through those repositories by oldest last run. A shared Project
therefore costs one full read per interval rather than one per repository, at
the price of each sharing repository being reconciled less often. A failed
refresh does not count, so it never defers a sibling.

`kanbanlan worker status` lists `problems`: duplicate registrations of one
repository (with the root the worker services), and registrations whose root
no longer exists or is not a Git checkout. `kanbanlan doctor` prints the same
problems as warnings. Each repository entry also reports `root_state`,
`last_activity_at`, `duplicate_skipped`, and `last_graphql_points`, the
GraphQL points its last worker run reported spending (a mutation that reports
no cost counts as one point).

## One worker process

Only the process holding `worker.lock` in the state directory runs cycles.
`worker start` returns the live worker instead of launching another, and a
`worker run` that finds a live holder exits at once. The running worker
re-checks and touches its lock after every sleep; if another live worker has
taken the lock it exits, and if the lock was swept it takes it back. Lock
ownership checks read the owner's age with `ps -o etime=` where `etimes` is
unavailable (macOS), so a long-running worker is never mistaken for a stale
one.

## macOS LaunchAgent

Create `/Users/YOU/Library/LaunchAgents/com.kanbanlan.worker.plist`, replacing
`YOU` and the executable path with the values reported by `id -un` and
`command -v kanbanlan`. Launchd does not expand `~` or use an interactive shell
PATH in these fields.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kanbanlan.worker</string>
  <key>ProgramArguments</key>
  <array><string>/Users/YOU/.local/bin/kanbanlan</string><string>worker</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/kanbanlan-worker.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/kanbanlan-worker.log</string>
</dict></plist>
```

Load it with
`launchctl bootstrap gui/$(id -u) /Users/YOU/Library/LaunchAgents/com.kanbanlan.worker.plist`
and unload it with
`launchctl bootout gui/$(id -u) /Users/YOU/Library/LaunchAgents/com.kanbanlan.worker.plist`,
then inspect health with `kanbanlan worker status`.

## Linux systemd user service

Create `~/.config/systemd/user/kanbanlan-worker.service`:

```ini
[Unit]
Description=Kanbanlan background reconciliation

[Service]
ExecStart=%h/.local/bin/kanbanlan worker run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

Run `systemctl --user daemon-reload` and
`systemctl --user enable --now kanbanlan-worker.service`. Keep GitHub CLI
credentials available through the platform credential store or an explicitly
scoped environment file; never put a token in the registry or unit file.
