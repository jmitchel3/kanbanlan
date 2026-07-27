# Default init to the Kanbanlan Project template

- Kanbanlan: `KBL-WGBPBHU7ONCHDPBELMTPCJLKEQ`
- Canonical home: `github`
- Canonical request: [#4](https://github.com/jmitchel3/kanbanlan/issues/4)

## Request

## Outcome

Running plain `kanbanlan init` creates a fresh GitHub Project by copying `https://github.com/users/jmitchel3/projects/6/views/5` and titles it `<current repository name> Delivery`. Explicit existing-project, blank-project, and alternate-template options continue to work.

## Acceptance criteria

- Plain interactive and non-interactive init copy jmitchel3 Project 6.
- The default title is the current repository name plus `Delivery`.
- Explicit project source and project title options retain precedence.
- CLI tests and README document the default behavior.

## Out of scope

Changing the contents of the canonical template Project.

## Decisions

- Plain interactive initialization keeps the Project selection menu but makes a
  fresh copy of `jmitchel3/6` its first and default choice.
- Plain non-interactive initialization copies the same template without listing
  or implicitly reusing existing Projects.
- Explicit existing-Project, empty-Project, alternate-template, and title flags
  retain precedence over the defaults.

## Verification

- `uv run pytest -q` — 70 tests and 4 subtests passed.
- `uv run ruff check .` — passed.
- `uv run ruff format --check .` — all 35 files formatted.
- `git diff --check` — passed.
- `uv run kanbanlan init --help` — confirmed the default-template behavior and
  explicit empty/alternate Project options are described in CLI help.

## Delivered result

Plain `kanbanlan init` now defaults to a fresh copy of the Project at
`https://github.com/users/jmitchel3/projects/6/views/5`, titled
`<repository name> Delivery`. The interactive wizard selects that path by
default, non-interactive initialization uses it without requiring a source
flag, and explicit existing, empty, alternate-template, and title selections
continue to override the defaults. README and CLI help describe the behavior.
