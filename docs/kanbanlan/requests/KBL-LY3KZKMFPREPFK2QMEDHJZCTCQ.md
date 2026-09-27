# Make reconciliation respect GitHub rate-limit cooldowns

- Kanbanlan: `KBL-LY3KZKMFPREPFK2QMEDHJZCTCQ`
- Canonical home: `github`
- Canonical request: [#78](https://github.com/jmitchel3/kanbanlan/issues/78)

## Request

Outcome: Reconciliation stops spending quota below the configured floor and workers sharing a GitHub account wait together until its quota resets. Recognize GitHub API rate limit already exceeded failures as throttling. Preserve the last good snapshot and report a reset time instead of treating stale data as a successful live reconciliation. Cover CLI/live reads, worker account isolation, reset recovery, and the observed error wording with regression tests.

## Decisions

- Check the last recorded quota before either reconciliation read, and again
  inside the snapshot refresh lock. Live reconciliation fails with a typed
  cooldown error rather than using stale data to propose or apply repairs.
- Keep the existing 500-point reserve and reuse GitHub's recorded reset time.
  Persist upstream refusals in cache health so repeated commands wait even
  when the last successful snapshot still reports plenty of points. With no
  usable reset time, defer for one minute without making a diagnostic API call.
- Share worker cooldowns by GitHub host and resolved account, using existing
  registry failure metadata. Persist the account that failed so a later
  binding change cannot attribute its cooldown to a different account.
- Recognize the observed `API rate limit already exceeded` diagnostic. Also
  classify rate limits from the issue-list command, which does not pass
  through the provider's GraphQL wrapper.
- This bounds retries and preserves quota based on known state. It does not
  introduce a global quota broker for unrelated foreground processes or
  change Project query hydration costs.

## Verification

- Full suite: 417 tests and 40 subtests passed using the repository's existing
  virtual environment with this worktree's `src` on `PYTHONPATH`.
- Ruff lint and format checks and `git diff --check` passed.
- `uv build --out-dir /private/tmp/kanbanlan-rate-limits-dist` produced both
  the source distribution and wheel.
- Regression coverage verifies zero provider calls below the reserve, both
  CLI reconcile modes, preserved last-good snapshots, cooldown reuse across
  cache/worker instances, reset recovery, account/host isolation, account
  rebinding, and the exact GitHub failure wording.
- Diagnosis found ten worker registrations using the affected account and
  repeated rate-limit failures treated as ordinary command errors. Existing
  snapshots recorded remaining quotas below the configured reserve while
  live reconciliation continued fetching. A small authenticated GraphQL read
  confirmed the account's quota recovered when its window reset.

## Delivered result

Live reconciliation and explicit refresh honor the quota reserve and persisted
cooldowns. The worker pauses jobs sharing a throttled account until retry is
allowed, while unrelated accounts continue. Cached status remains available.
This is a source patch; the installed CLI and running worker require an upgrade
and restart after delivery.
