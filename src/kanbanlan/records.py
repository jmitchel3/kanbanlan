from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kanbanlan.domain import KanbanlanRequest
from kanbanlan.identity import strip_kanbanlan_metadata

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
