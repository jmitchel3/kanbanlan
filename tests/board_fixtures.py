"""Shared request and snapshot builders for command tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from kanbanlan.config import Config
from kanbanlan.snapshot import SCHEMA_VERSION, isoformat

REPOSITORY = "acme/widget"
ALPHA = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
BETA = "KBL-BBBBBBBBBBBBBBBBBBBBBBBBBB"


def config() -> Config:
    return Config(
        repository=REPOSITORY,
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def item(
    number: int,
    kanbanlan_id: str,
    status: str,
    *,
    claim: dict[str, Any] | None = None,
    pull_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "ISSUE",
        "kanbanlan_id": kanbanlan_id,
        "provider": "github",
        "provider_ref": f"github:{REPOSITORY}#{number}",
        "display_id": f"#{number}",
        "project_item_id": f"item-{number}",
        "number": number,
        "title": f"Request {number}",
        "repository": REPOSITORY,
        "state": "OPEN",
        "status": status,
        "priority": "priority:p2",
        "active_claim": claim,
        "session_history": [],
        "linked_open_pull_requests": pull_requests or [],
    }


def snapshot(items: list[dict[str, Any]], *, age_seconds: float = 0) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": isoformat(datetime.now(UTC) - timedelta(seconds=age_seconds)),
        "source": {"repository": REPOSITORY},
        "project": {"id": "project-1"},
        "items": items,
        "status_counts": {},
        "ready_cards": [],
        "next_ready": None,
        "open_pull_requests": [],
        "rate_limit": {},
    }
