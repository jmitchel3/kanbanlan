from __future__ import annotations

import base64
import re
import uuid

KANBANLAN_ID_PREFIX = "KBL-"
KANBANLAN_ID_PATTERN = r"KBL-[A-Z2-7]{26}"
KANBANLAN_ID_RE = re.compile(rf"\b(?P<id>{KANBANLAN_ID_PATTERN})\b", re.IGNORECASE)
KANBANLAN_MARKER_RE = re.compile(
    rf"<!--\s*kanbanlan:id=(?P<id>{KANBANLAN_ID_PATTERN})\s*-->",
    re.IGNORECASE,
)
KANBANLAN_LINE_RE = re.compile(
    rf"^Kanbanlan:\s*`?(?P<id>{KANBANLAN_ID_PATTERN})`?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def new_kanbanlan_id() -> str:
    """Return a globally unique, provider-independent Kanbanlan ID."""

    token = base64.b32encode(uuid.uuid4().bytes).decode("ascii").rstrip("=")
    return f"{KANBANLAN_ID_PREFIX}{token}"


def normalize_kanbanlan_id(value: str) -> str | None:
    match = KANBANLAN_ID_RE.fullmatch(value.strip())
    return match.group("id").upper() if match else None


def extract_kanbanlan_id(value: str | None) -> str | None:
    if not value:
        return None
    marker = KANBANLAN_MARKER_RE.search(value)
    if marker:
        return marker.group("id").upper()
    line = KANBANLAN_LINE_RE.search(value)
    if line:
        return line.group("id").upper()
    return None


def find_kanbanlan_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return sorted({match.group("id").upper() for match in KANBANLAN_ID_RE.finditer(value)})


def attach_kanbanlan_id(body: str, kanbanlan_id: str) -> str:
    normalized = normalize_kanbanlan_id(kanbanlan_id)
    if normalized is None:
        raise ValueError(f"invalid Kanbanlan ID: {kanbanlan_id!r}")
    existing = extract_kanbanlan_id(body)
    if existing:
        if existing != normalized:
            raise ValueError(f"request already carries Kanbanlan ID {existing}")
        return body
    content = body.lstrip("\n")
    return f"<!-- kanbanlan:id={normalized} -->\nKanbanlan: `{normalized}`\n\n{content}"


def strip_kanbanlan_metadata(body: str | None) -> str:
    if not body:
        return ""
    value = KANBANLAN_MARKER_RE.sub("", body)
    value = KANBANLAN_LINE_RE.sub("", value)
    return value.strip()
