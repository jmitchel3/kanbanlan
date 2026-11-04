from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kanbanlan.domain import KanbanlanRequest
from kanbanlan.identity import strip_kanbanlan_metadata
from kanbanlan.runner import CommandError, Runner

RECORD_DIRECTORY = Path("docs") / "kanbanlan" / "requests"


@dataclass(frozen=True)
class RecordResult:
    path: Path
    action: str


def record_path(root: Path, kanbanlan_id: str) -> Path:
    return root / RECORD_DIRECTORY / f"{kanbanlan_id}.md"


def render_record(item: dict[str, Any]) -> str:
    request = KanbanlanRequest.from_snapshot_item(item)
    if not request.kanbanlan_id:
        raise RuntimeError("request has no Kanbanlan ID; run 'kanbanlan reconcile --apply'")
    canonical = request.url or request.provider_ref
    request_body = strip_kanbanlan_metadata(request.body) or "_No request body was recorded._"
    return f"""# {request.title}

- Kanbanlan: `{request.kanbanlan_id}`
- Canonical home: `{request.provider}`
- Canonical request: [{request.display_id}]({canonical})

## Request

{request_body}

## Decisions

<!-- Record durable implementation decisions that are not obvious from the code. -->

## Verification

<!-- Record automated and manual evidence collected before delivery. -->

## Delivered result

<!-- Summarize what changed and any follow-up work that remains. -->
"""


def create_record(root: Path, item: dict[str, Any]) -> RecordResult:
    request = KanbanlanRequest.from_snapshot_item(item)
    if not request.kanbanlan_id:
        raise RuntimeError("request has no Kanbanlan ID; run 'kanbanlan reconcile --apply'")
    path = record_path(root, request.kanbanlan_id)
    if path.exists():
        return RecordResult(path, "unchanged")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_record(item), encoding="utf-8")
    return RecordResult(path, "created")


# ---------------------------------------------------------------------------
# Records generated from the pull request
#
# The pull request body is the one place an agent writes decisions,
# verification, and the delivered result. ``record --from-pr`` reads those
# sections back and renders them into the record format above, so the record
# never repeats what the agent typed by hand somewhere else.

# Record heading for each pull request section, keyed by the accepted
# pull request headings (compared case-insensitively).
PULL_REQUEST_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Decisions", ("decisions",)),
    ("Verification", ("verification",)),
    ("Delivered result", ("result", "delivered result")),
)

_HEADING = re.compile(r"^(#{1,2})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class PullRequestRecord:
    text: str
    missing: tuple[str, ...]


def pull_request_sections(body: str | None) -> dict[str, str]:
    """Split a pull request body into its level-two sections.

    Keys are lowercased headings. HTML comments (template guidance) are
    removed, and a heading inside a fenced code block does not start a
    section. A level-one heading ends the current section without starting a
    tracked one.
    """

    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    fenced = False
    for line in _COMMENT.sub("", body or "").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match:
            current = None
            if len(match.group(1)) == 2:
                current = sections.setdefault(match.group(2).strip().lower(), [])
            continue
        if current is not None:
            current.append(line.rstrip())
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def render_record_from_pull_request(
    item: dict[str, Any],
    pull_request: dict[str, Any],
) -> PullRequestRecord:
    """Render a record whose delivery sections come from ``pull_request``.

    A section that is absent or holds only template guidance is reported in
    ``missing`` and left as a placeholder, never filled in.
    """

    request = KanbanlanRequest.from_snapshot_item(item)
    if not request.kanbanlan_id:
        raise RuntimeError("request has no Kanbanlan ID; run 'kanbanlan reconcile --apply'")
    canonical = request.url or request.provider_ref
    request_body = strip_kanbanlan_metadata(request.body) or "_No request body was recorded._"
    sections = pull_request_sections(pull_request.get("body"))
    number = pull_request.get("number")
    label = f"#{number}" if number is not None else "pull request"
    lines = [
        f"# {request.title}",
        "",
        f"- Kanbanlan: `{request.kanbanlan_id}`",
        f"- Canonical home: `{request.provider}`",
        f"- Canonical request: [{request.display_id}]({canonical})",
    ]
    if pull_request.get("url"):
        lines.append(f"- Pull request: [{label}]({pull_request['url']})")
    lines += ["", "## Request", "", request_body]
    missing: list[str] = []
    for heading, names in PULL_REQUEST_SECTIONS:
        content = next((sections[name] for name in names if sections.get(name)), "")
        if not content:
            missing.append(heading)
            content = f"<!-- Missing from {label}: add a '{names[0].title()}' section. -->"
        lines += ["", f"## {heading}", "", content]
    return PullRequestRecord("\n".join(lines) + "\n", tuple(missing))


def write_record_from_pull_request(
    root: Path,
    item: dict[str, Any],
    pull_request: dict[str, Any],
) -> tuple[RecordResult, tuple[str, ...]]:
    """Write or update the record from ``pull_request`` and report gaps."""

    rendered = render_record_from_pull_request(item, pull_request)
    path = record_path(root, str(item.get("kanbanlan_id")))
    if path.exists():
        if path.read_text(encoding="utf-8") == rendered.text:
            return RecordResult(path, "unchanged"), rendered.missing
        action = "updated"
    else:
        action = "created"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered.text, encoding="utf-8")
    return RecordResult(path, action), rendered.missing


def select_pull_request(
    item: dict[str, Any],
    branch: str | None,
) -> dict[str, Any]:
    """Pick the open pull request that delivers ``item``.

    The pull request whose head is ``branch`` wins; otherwise a single linked
    pull request is used. No candidate, or several with none on ``branch``,
    is an error rather than a guess.
    """

    candidates = list(item.get("linked_open_pull_requests") or [])
    on_branch = [value for value in candidates if branch and value.get("head_ref") == branch]
    if len(on_branch) == 1:
        return on_branch[0]
    if len(candidates) == 1:
        return candidates[0]
    reference = item.get("kanbanlan_id") or item.get("provider_ref")
    if not candidates:
        raise RuntimeError(
            f"no open pull request is linked to {reference}; open one that carries "
            "its Kanbanlan ID or closes its issue"
        )
    listed = ", ".join(str(value.get("url")) for value in candidates)
    raise RuntimeError(
        f"several open pull requests are linked to {reference} and none is on the "
        f"current branch: {listed}"
    )


def current_branch(root: Path) -> str | None:
    """Return the branch checked out at ``root``, or None when detached."""

    try:
        result = Runner(cwd=root).run(["git", "branch", "--show-current"], check=False)
    except CommandError:
        return None
    if result.returncode:
        return None
    return result.stdout.strip() or None
