# Choose a gh account by repository access when no account owns the repository

- Kanbanlan: `KBL-HI4DT6QJBNDPXOJ3LNDAHXPSMQ`
- Canonical home: `github`
- Canonical request: [#131](https://github.com/jmitchel3/kanbanlan/issues/131)
- Pull request: [#132](https://github.com/jmitchel3/kanbanlan/pull/132)

## Request

Kanbanlan could not choose a gh account for a repository owned by an
organization that no logged-in account is named after (for example
`paracord-clients/beehaveserviceteam.com` with five accounts logged in), even
though gh's active account could push to it. Every command failed with
"none owns it".

## Outcome

When no binding, owner match, or single login decides, Kanbanlan checks which
logged-in accounts can push to the repository and chooses one only when the
answer is unambiguous.

## Acceptance criteria

- [x] gh's active account is chosen when it can push, probed first and alone.
- [x] Otherwise the single account that can push is chosen.
- [x] Several pushers without the active one, or any probe that cannot be
  answered, is an error naming the accounts, never a guess.
- [x] Probes are bounded by a timeout and never put the token in argv.

## Decisions

- When no binding exists, no logged-in account is named after the repository or Project owner, and more than one account is logged in, each logged-in account is asked whether it can push to the repository (`gh api repos/OWNER/REPO --jq .permissions.push` with that account's token).
- gh's active account wins when it can push; otherwise the single account that can push is chosen. Several pushers without the active one is an error naming them, so the choice is never a guess.
- The probe runs only on this last fallback and its result is persisted as a repository binding like every automatic choice, so the cost is paid once. `admin account` (show) does not persist.
- Owner matches and single-account cases never probe.

## Verification

- 5 new tests in `tests/test_accounts.py` (active pusher chosen and persisted, sole pusher over the active one, several pushers error, owner match never probes, show does not persist).
- `uv run pytest`: 545 passed; ruff check and format clean.
- Live: `kanbanlan admin account` in `paracord-clients/beehaveserviceteam.com` (no binding; 5 accounts logged in, none named after the org) now resolves `j-paracord` with source "automatic: active account can push to the repository" instead of failing.

## Delivered result

Repositories owned by an organization resolve an account automatically when the access check is unambiguous. README resolution order updated.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
