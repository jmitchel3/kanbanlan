# done and start push and fetch with the bound gh account

- Kanbanlan: `KBL-T2OQEHT3OFCQXBAWW7RHXVZXFE`
- Canonical home: `github`
- Canonical request: [#123](https://github.com/jmitchel3/kanbanlan/issues/123)
- Pull request: [#127](https://github.com/jmitchel3/kanbanlan/pull/127)

## Request

In a repo bound to jmitchel3 while the active gh account is j-paracord, kanbanlan done failed: git push got 403 (Permission denied to j-paracord), and plain git fetch fails with Repository not found. GitHub API calls honour the repository binding, but git transport uses the gh credential helper with the active account. Every git subprocess Kanbanlan runs against the remote (fetch, push, ls-remote) must authenticate as the bound account, for example GH_TOKEN from gh auth token -u BOUND in that subprocess environment.

## Decisions

- `AccountRunner` now scopes git commands that reach a remote (`fetch`, `push`, `pull`, `ls-remote`, `clone`, `remote update|prune|show|set-head`, `submodule update|sync|add`), detected after skipping git global options such as `-C` and `-c`. Local git commands run untouched and never trigger account resolution.
- Scoping applies only when an account resolves (binding, owner match, or `KANBANLAN_GITHUB_ACCOUNT`) and it differs from gh's active account (`gh config get user --host`, which reads gh's config file with no network call), or when an ambient `GH_TOKEN`/`GITHUB_TOKEN` is set. With no resolved account, or when the bound account is already active, behaviour is unchanged.
- A scoped git command gets `GH_TOKEN` for the bound account and, through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`, an empty `credential.https://<host>.helper` (clearing keychain or other helpers) followed by `!gh auth git-credential`. Existing `GIT_CONFIG_*` entries are extended, not replaced. Nothing is written to git config, argv, or disk.
- The token is redacted as `[redacted]` from the stdout, stderr, and args of scoped results and of the `CommandError` they raise.
- If the account cannot be resolved (ambiguous login), git falls back to its own credentials; gh commands still report the resolution error.
- `done` pushes through its account runner; `start` fetches and creates the worktree through one; `cleanup` and orphan pruning fetch through one.

## Verification

- [x] `uv run pytest` (524 passed)
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv build`
- New tests in `tests/test_accounts.py` use a fake `git` and `gh` on `PATH` that record the subprocess environment: push, fetch, ls-remote, `-C`/`-c` prefixed commands are scoped; local commands are untouched and never resolve; bound equals active, no account, and unresolvable account leave git untouched; `KANBANLAN_GITHUB_ACCOUNT` is used; an ambient token is replaced; existing `GIT_CONFIG_*` is extended; failures never reveal the token.
- A real-git test runs `git credential fill` with a global config helper that answers with the wrong password and confirms the environment makes git ask gh, which answers with the bound token.
- The new and updated tests fail on `main` (import error and routing assertions in `test_verbs.py`/`test_cleanup.py`).
- Live: this PR's branch was pushed by `uv run kanbanlan done` from the worktree with gh's active account `j-paracord`, no `GH_TOKEN` override, to a repository bound to `jmitchel3`.

## Delivered result

`kanbanlan start`, `done`, and `cleanup` now fetch and push as the repository's bound gh account even when gh's active account is a different one, without writing or logging the token.
