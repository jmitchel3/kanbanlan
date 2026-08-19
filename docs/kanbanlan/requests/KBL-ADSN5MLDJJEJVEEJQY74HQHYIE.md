# Release Kanbanlan 0.8.0

- Kanbanlan: `KBL-ADSN5MLDJJEJVEEJQY74HQHYIE`
- Canonical home: `github`
- Canonical request: [#42](https://github.com/jmitchel3/kanbanlan/issues/42)

## Request

## Outcome

Kanbanlan 0.8.0 is published to PyPI and GitHub Releases from a tagged commit
on `main`, and a clean install reports the same version.

## Scope

Carries the four multi-repository requests delivered to `main` since 0.7.0:

- #34 project-wide read and overlap scope for shared Projects
- #37 cross-repository pull request recognition
- #35 explicit repository routing when capturing a request
- #36 rehoming a request without changing its Kanbanlan identity

Minor rather than patch: all four add functionality, and repository precedent
bumps the minor for a feature before 1.0.

## Acceptance criteria

- [ ] `pyproject.toml`, `src/kanbanlan/__init__.py`, and `uv.lock` agree on 0.8.0
- [ ] `SECURITY.md` names the current supported release line
- [ ] `uv lock --check`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv build` pass
- [ ] CI is green on the release commit before the tag is created
- [ ] Tag `v0.8.0` and a published GitHub Release target that commit
- [ ] PyPI reports 0.8.0 and `uvx --from kanbanlan==0.8.0 kanbanlan --version` agrees

## Decisions

Minor, not patch. All four requests delivered since 0.7.0 add functionality,
and repository precedent bumps the minor for a feature before 1.0 (#21 shipped
as 0.6.0, #29 as 0.7.0).

Minor, not major, despite behavior changes visible to consumers. Before 1.0 a
minor bump is the documented choice for substantial change, and the changes
that could surprise an existing consumer are contained:

- snapshot `schema_version` moves from 2 to 3, which only invalidates local
  caches; they refresh automatically;
- `display_id` qualifies peer-repository content that repository scope never
  emitted before, so no previously emitted value changed;
- a pull request that mentions several Kanbanlan IDs without declaring one no
  longer links to all of them. It links to none and is reported. This is the
  one behavior an existing repository could notice, and the `Kanbanlan:` line
  the generated pull request template already asks for keeps it working.

`SECURITY.md` names the supported release line, so it moves with the version
rather than being revisited later.

## Verification

Full release gate on the release commit:

- `uv lock --check` clean
- `uv run pytest` 238 passed, 28 subtests
- `uv run ruff check .` clean
- `uv run ruff format --check .` clean, 65 files
- `uv build` produced `kanbanlan-0.8.0.tar.gz` and
  `kanbanlan-0.8.0-py3-none-any.whl`

Version agreement confirmed across `pyproject.toml`,
`src/kanbanlan/__init__.py`, `uv.lock`, the wheel `METADATA`
(`Version: 0.8.0`), and `kanbanlan --version`.

Every workflow and issue-form YAML file under `.github/` parses.
`release.yaml` keeps PyPI authentication secretless: OIDC Trusted Publishing
through the `pypi` environment, `id-token: write` granted only to the publish
job, `timeout-minutes` on both jobs, and no `skip-existing`.

Tracked and untracked files were scanned for credentials before staging.

Post-publish verification is recorded in the delivered result.

## Delivered result

Kanbanlan 0.8.0, carrying the four multi-repository requests delivered to
`main` since 0.7.0:

- #34 an explicit read-only project scope, `kanbanlan overlap`,
  `status --project`, and `snapshot --project`, with repository-qualified
  identity throughout
- #37 recognition of a cross-repository pull request that closes a request by
  qualified reference or declares its Kanbanlan ID, with ambiguity reported in
  `linkage_problems`
- #35 `kanbanlan capture --repository OWNER/REPO`, with the target validated
  and prepared before anything is created
- #36 `kanbanlan rehome`, a plan-by-default move that preserves the Kanbanlan
  ID, discussion, and Project state

Together these make one GitHub Project usable across several repositories: an
agent can perform the cross-repository overlap check its instructions already
require, a request can be captured into or moved to the repository that will
implement it, and the pull request that actually delivers it is recognized
wherever it lives.
