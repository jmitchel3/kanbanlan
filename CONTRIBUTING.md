# Contributing to Kanbanlan

Thanks for helping improve Kanbanlan. Bug reports, documentation fixes, and
small, focused feature changes are welcome.

## Development setup

Kanbanlan requires Python 3.11 or newer, Git, GitHub CLI, and uv.

```sh
git clone https://github.com/jmitchel3/kanbanlan.git
cd kanbanlan
uv sync --group dev
```

Run the same checks used in CI before opening a pull request:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

## Proposing a change

1. Open an issue for bugs or substantial changes so the intended behavior can
   be agreed on before implementation.
2. Keep each pull request focused on one outcome and add or update tests for
   behavior changes.
3. Update user-facing documentation when commands, configuration, or workflow
   behavior changes.
4. Explain the change and verification performed in the pull request template.

Never include GitHub tokens, repository snapshots, or `.kanbanlan.toml` files
from private repositories in reports or fixtures.

## Releasing

Releases use PyPI Trusted Publishing, so no long-lived PyPI token is stored in
GitHub. Before the first release:

1. Create a GitHub environment named `pypi` and add any desired deployment
   reviewers.
2. Configure the PyPI Trusted Publisher for owner `jmitchel3`, repository
   `kanbanlan`, workflow `release.yaml`, and environment `pypi`.

To publish a release, update the version in `pyproject.toml` and `uv.lock`, then
publish a GitHub Release whose tag is `v` followed by that exact version, such
as tag `v1.2.3` for package version `1.2.3`. The release workflow builds the
distributions in an unprivileged job and publishes them from the protected
`pypi` environment.
