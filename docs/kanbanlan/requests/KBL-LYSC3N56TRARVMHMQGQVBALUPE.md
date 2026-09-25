# Bind every GitHub call to an explicit gh account instead of the active one

- Kanbanlan: `KBL-LYSC3N56TRARVMHMQGQVBALUPE`
- Canonical home: `github`
- Canonical request: [#66](https://github.com/jmitchel3/kanbanlan/issues/66)

## Request

## Outcome

Kanbanlan runs every GitHub read and write, foreground and background, as a deterministic gh account for the repository instead of whichever account is active in gh. With several accounts logged in (the active one often belongs to a different owner), writes today can land under the wrong identity or fail on access.

## Acceptance criteria

- [x] Account resolution order: KANBANLAN_GITHUB_ACCOUNT, then a user-level binding for the repository or its owner, then an automatic choice only when it is unambiguous (the repository or Project owner is a logged-in account, or exactly one account is logged in); otherwise a clear error naming the logged-in accounts.
- [x] A command to set, show, and clear the binding (kanbanlan account).
- [x] GitHub calls carry GH_TOKEN for the bound account; the worker uses the same resolver.
- [x] Automatic choices are persisted so later commands pay only the token lookup.
- [x] Tests cover resolution order, ambiguity, and token scoping.

## Decisions

- Resolution lives in `kanbanlan.accounts`: `KANBANLAN_GITHUB_ACCOUNT`,
  then a user-level binding for the repository, then for its owner
  (`accounts.json` in the Kanbanlan state directory, mode 0600, written
  under a `FileLock`), then an automatic choice only when unambiguous: the
  repository owner or the Project owner is itself a logged-in account, or
  exactly one account is logged in. Anything else fails with the logged-in
  accounts named and the `kanbanlan account use` hint. gh's active account
  is never consulted, because it is whichever account was used last.
- An automatic choice is persisted as a repository binding, so its
  `gh auth status` call (about 1.2s, it validates every token) is paid
  once; afterwards a command pays only `gh auth token --user` (about 50ms),
  once per process.
- An explicit `GH_TOKEN` or `GITHUB_TOKEN` is honored when nothing is bound
  or requested (CI, scoped tokens), since it already names one identity. A
  binding still wins over it.
- `GitHub` gets an `AccountRunner` whenever it has a configuration. The
  runner resolves lazily on the first gh command, under a lock because
  board reads issue gh commands from several threads, and injects
  `GH_HOST`/`GH_TOKEN` while dropping `GITHUB_TOKEN` and
  `GH_ENTERPRISE_TOKEN`; git commands run with the environment untouched.
  `init` and `auth` (no configuration yet) keep gh's own account.
- The worker's `scoped_runner` prefers the binding over the login recorded
  at registration, so rebinding takes effect on the next cycle. Worker
  registration now resolves through the same rules instead of
  `gh api user` (the active account); `worker enable --github-login` and
  `account use` write the binding and keep the registration in step.
- Only gh calls are bound. `git fetch` and `git push` still use git's
  credential helper, which is outside this request.

## Verification

- `uv run pytest`: 369 passed, 37 subtests; ruff check and format clean.
  `tests/test_accounts.py` covers the resolution order, owner and
  Project-owner matches, a single account, ambiguity and no-account
  errors, environment tokens, persistence (and none on `show`), unbinding,
  token scoping for gh versus git, and single resolution under concurrent
  gh calls. The test suite isolates `KANBANLAN_STATE_DIR` so it never reads
  real bindings.
- Live, with `j-paracord` active and `jmitchel3` also logged in: `kanbanlan
  account` resolved `jmitchel3` ("owner is a logged-in account"), the first
  refresh persisted it, and `kanbanlan account` then reported "repository
  binding". Warm `refresh` stayed at 1.56 to 1.60s. `worker status` showed
  this repository registered under `j-paracord`, the misattribution this
  fixes.

## Delivered result

Every GitHub call made with a repository configuration, foreground or
worker, now acts as a deterministic gh account, set with `kanbanlan
account use LOGIN [--owner]`, shown with `kanbanlan account`, and cleared
with `kanbanlan account clear`.
