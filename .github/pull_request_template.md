<!--
The Decisions, Verification, and Result sections are the single place to write
them. `kanbanlan record <Kanbanlan ID> --from-pr` generates the durable request
record from this body; commit that record in this pull request.
-->

## Request

Kanbanlan: KBL-

Closes #

## Decisions

<!-- Durable implementation decisions that are not obvious from the code. -->

## Verification

<!-- List automated checks and relevant manual verification. -->

- [ ] `uv run pytest`
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv build`

## Result

<!-- What this pull request delivers, and any follow-up work that remains. -->

## Checklist

- [ ] Tests and documentation were updated where needed.
- [ ] `docs/kanbanlan/requests/<Kanbanlan ID>.md` was generated with `kanbanlan record --from-pr`.

## Security and compatibility

<!-- Note credential handling, GitHub API changes, compatibility risks, or write "None". -->
