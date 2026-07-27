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

## macOS LaunchAgent

Create `~/Library/LaunchAgents/com.kanbanlan.worker.plist`, replacing the
Python path with the active installation:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kanbanlan.worker</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/env</string><string>kanbanlan</string><string>worker</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>~/Library/Logs/kanbanlan-worker.log</string>
  <key>StandardErrorPath</key><string>~/Library/Logs/kanbanlan-worker.log</string>
</dict></plist>
```

Load or unload it with `launchctl bootstrap gui/$(id -u)` and
`launchctl bootout gui/$(id -u)`, then inspect health with
`kanbanlan worker status`.

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
