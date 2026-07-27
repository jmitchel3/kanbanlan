from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kanbanlan.config import Config
from kanbanlan.identity import extract_kanbanlan_id, find_kanbanlan_ids

SCHEMA_VERSION = 2
CLAIM_RE = re.compile(r"^CLAIM:\s*(?P<claimed_at>[^\n]+)", re.MULTILINE)
CLAIM_FIELD_RE = re.compile(
    r"^(Session|Branch|Worktree|Touchpoints):\s*(.+)$",
    re.MULTILINE,
)
PRIORITY_ORDER = {
    "priority:p0": 0,
    "priority:p1": 1,
    "priority:p2": 2,
    "priority:p3": 3,
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _field_value(item: dict[str, Any], field_name: str) -> str | None:
    for field_value in item.get("fieldValues", {}).get("nodes", []):
        if field_value.get("field", {}).get("name") == field_name:
            return field_value.get("name")
    return None


def _labels(content: dict[str, Any]) -> list[dict[str, str]]:
    return sorted(
        content.get("labels", {}).get("nodes", []),
        key=lambda label: label["name"],
    )


def _comment_fields(body: str) -> dict[str, str]:
    return {name.lower(): value.strip() for name, value in CLAIM_FIELD_RE.findall(body)}


def active_claim(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    active: dict[str, Any] | None = None
    for comment in sorted(comments, key=lambda value: value.get("createdAt", "")):
        body = comment.get("body") or ""
        author = (comment.get("author") or {}).get("login")
        fields = _comment_fields(body)
        if body.startswith("RELEASED:"):
            if active and (
                (fields.get("session") and fields["session"] == active.get("session"))
                or (not fields.get("session") and author == active.get("author"))
            ):
                active = None
            continue
        if body.startswith("HANDOFF:") and active and fields.get("session"):
            active = {
                **active,
                **fields,
                "author": author,
                "commented_at": comment.get("createdAt"),
            }
            continue
        match = CLAIM_RE.search(body)
        if match and active is None:
            active = {
                "claimed_at": match.group("claimed_at").strip(),
                "commented_at": comment.get("createdAt"),
                "author": author,
                **fields,
            }
    return active


def _normalize_pull_request(pull_request: dict[str, Any], repository: str) -> dict[str, Any]:
    closing_numbers = []
    for issue in pull_request.get("closingIssuesReferences", {}).get("nodes", []):
        issue_repository = issue.get("repository", {}).get("nameWithOwner")
        if issue_repository == repository:
            closing_numbers.append(issue["number"])
    return {
        "number": pull_request["number"],
        "title": pull_request["title"],
        "body": pull_request.get("body"),
        "url": pull_request["url"],
        "head_ref": pull_request.get("headRefName"),
        "base_ref": pull_request.get("baseRefName"),
        "is_draft": pull_request.get("isDraft", False),
        "merge_state": pull_request.get("mergeStateStatus"),
        "created_at": pull_request.get("createdAt"),
        "updated_at": pull_request.get("updatedAt"),
        "author": (pull_request.get("author") or {}).get("login"),
        "labels": _labels(pull_request),
        "closing_issue_numbers": sorted(closing_numbers),
        "kanbanlan_ids": find_kanbanlan_ids(pull_request.get("body")),
    }


def _merge_pull_requests(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for group in groups:
        for pull_request in group:
            by_url[pull_request["url"]] = pull_request
    return sorted(by_url.values(), key=lambda value: value.get("number", 0))


def build_snapshot(
    config: Config,
    project: dict[str, Any],
    raw_pull_requests: list[dict[str, Any]],
    rate_limit: dict[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or utc_now()
    pull_requests = [
        _normalize_pull_request(value, config.repository) for value in raw_pull_requests
    ]
    linked_pull_requests: dict[int, list[dict[str, Any]]] = {}
    linked_pull_requests_by_kanbanlan_id: dict[str, list[dict[str, Any]]] = {}
    for pull_request in pull_requests:
        for issue_number in pull_request["closing_issue_numbers"]:
            linked_pull_requests.setdefault(issue_number, []).append(pull_request)
        for kanbanlan_id in pull_request["kanbanlan_ids"]:
            linked_pull_requests_by_kanbanlan_id.setdefault(kanbanlan_id, []).append(pull_request)

    items: list[dict[str, Any]] = []
    for raw_item in project.get("items", []):
        if raw_item.get("isArchived"):
            continue
        content = raw_item.get("content") or {}
        item_type = raw_item.get("type", "UNKNOWN")
        content_repository = content.get("repository", {}).get("nameWithOwner")
        if item_type in {"ISSUE", "PULL_REQUEST"} and content_repository != config.repository:
            continue
        if item_type == "DRAFT_ISSUE":
            continue
        status = _field_value(raw_item, "Status")
        normalized: dict[str, Any] = {
            "project_item_id": raw_item.get("id"),
            "type": item_type,
            "status": status,
            "title": content.get("title"),
            "body": content.get("body"),
            "url": content.get("url"),
            "created_at": content.get("createdAt"),
            "updated_at": content.get("updatedAt"),
        }
        if item_type == "ISSUE":
            labels = _labels(content)
            label_names = [label["name"] for label in labels]
            priority = next(
                (label for label in label_names if label in PRIORITY_ORDER),
                None,
            )
            number = content["number"]
            kanbanlan_id = extract_kanbanlan_id(content.get("body"))
            provider_ref = f"github:{content_repository}#{number}"
            normalized.update(
                {
                    "kanbanlan_id": kanbanlan_id,
                    "provider": "github",
                    "provider_id": content.get("id"),
                    "provider_ref": provider_ref,
                    "display_id": f"#{number}",
                    "canonical_url": content.get("url"),
                    "content_id": content.get("id"),
                    "number": number,
                    "repository": content_repository,
                    "state": content.get("state"),
                    "state_reason": content.get("stateReason"),
                    "closed_at": content.get("closedAt"),
                    "labels": labels,
                    "priority": priority,
                    "assignees": sorted(
                        assignee["login"]
                        for assignee in content.get("assignees", {}).get("nodes", [])
                    ),
                    "active_claim": (
                        active_claim(content.get("comments", {}).get("nodes", []))
                        if content.get("state") == "OPEN"
                        else None
                    ),
                    "linked_open_pull_requests": _merge_pull_requests(
                        linked_pull_requests.get(number, []),
                        linked_pull_requests_by_kanbanlan_id.get(kanbanlan_id, [])
                        if kanbanlan_id
                        else [],
                    ),
                }
            )
        elif item_type == "PULL_REQUEST":
            normalized.update(
                {
                    "content_id": content.get("id"),
                    "number": content.get("number"),
                    "repository": content_repository,
                    "state": content.get("state"),
                    "is_draft": content.get("isDraft", False),
                    "merged_at": content.get("mergedAt"),
                }
            )
        items.append(normalized)

    status_counts: dict[str, int] = {}
    for item in items:
        status = item.get("status") or "Unspecified"
        status_counts[status] = status_counts.get(status, 0) + 1

    ready = [
        item
        for item in items
        if item["type"] == "ISSUE"
        and item.get("state") == "OPEN"
        and item.get("status") == "Ready"
        and not item.get("active_claim")
    ]
    ready.sort(
        key=lambda item: (
            PRIORITY_ORDER.get(item.get("priority"), len(PRIORITY_ORDER)),
            item.get("number", sys.maxsize),
        )
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": isoformat(generated_at),
        "fresh_for_seconds": config.stale_seconds,
        "source": {
            "repository": config.repository,
            "project_owner": config.project_owner,
            "project_number": config.project_number,
            "authoritative": "github",
            "code_host": config.code_host,
            "canonical_home": config.canonical_home,
            "projections": list(config.projections),
        },
        "project": {
            "id": project.get("id"),
            "number": project.get("number"),
            "title": project.get("title"),
            "url": project.get("url"),
            "updated_at": project.get("updatedAt"),
            "fields": project.get("fields", {}),
        },
        "status_counts": dict(sorted(status_counts.items())),
        "items": items,
        "ready_cards": ready,
        "next_ready": ready[0] if ready else None,
        "open_pull_requests": pull_requests,
        "rate_limit": rate_limit,
    }


class FileLock:
    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    stale = time.time() - self.path.stat().st_mtime > 60
                except FileNotFoundError:
                    continue
                if stale:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"timed out waiting for cache lock {self.path}")
                time.sleep(0.05)

    def __exit__(self, *_args: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class CacheStore:
    def __init__(self, config: Config, directory: Path):
        self.config = config
        self.directory = directory
        self.snapshot_path = directory / "snapshot.json"
        self.health_path = directory / "health.json"
        self.lock_path = directory / "refresh.lock"

    def refresh(self, client: Any) -> dict[str, Any]:
        self._prepare_directory()
        with FileLock(self.lock_path):
            attempted_at = utc_now()
            try:
                if hasattr(client, "snapshot"):
                    snapshot = client.snapshot(generated_at=attempted_at)
                else:
                    project, pull_requests, rate_limit = client.fetch()
                    snapshot = build_snapshot(
                        self.config,
                        project,
                        pull_requests,
                        rate_limit,
                        generated_at=attempted_at,
                    )
                self._write_json(self.snapshot_path, snapshot)
                self._write_json(
                    self.health_path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "last_attempt_at": isoformat(attempted_at),
                        "last_success_at": snapshot["generated_at"],
                        "refresh_status": "ok",
                        "error": None,
                    },
                )
                return snapshot
            except Exception as exc:
                self._write_failure_health(attempted_at, exc)
                raise

    def ensure(self, client: Any) -> dict[str, Any]:
        snapshot = self.snapshot()
        if self._snapshot_state(snapshot) == "fresh":
            assert snapshot is not None
            return snapshot
        return self.refresh(client)

    def inspect(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        health = self._read_json(self.health_path)
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_state": self._snapshot_state(snapshot),
            "cache_dir": str(self.directory),
            "snapshot_path": str(self.snapshot_path),
            "generated_at": snapshot.get("generated_at") if snapshot else None,
            "age_seconds": self._snapshot_age(snapshot),
            "refresh_status": health.get("refresh_status") if health else "unknown",
            "last_attempt_at": health.get("last_attempt_at") if health else None,
            "last_success_at": health.get("last_success_at") if health else None,
            "error": health.get("error") if health else None,
            "next_ready": snapshot.get("next_ready") if snapshot else None,
            "status_counts": snapshot.get("status_counts", {}) if snapshot else {},
        }

    def is_fresh(self) -> bool:
        return self._snapshot_state(self.snapshot()) == "fresh"

    def snapshot(self) -> dict[str, Any] | None:
        return self._read_json(self.snapshot_path)

    def _prepare_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    def _write_failure_health(self, attempted_at: datetime, error: Exception) -> None:
        prior_health = self._read_json(self.health_path)
        prior_snapshot = self.snapshot()
        last_success = (prior_health or {}).get("last_success_at") or (prior_snapshot or {}).get(
            "generated_at"
        )
        self._write_json(
            self.health_path,
            {
                "schema_version": SCHEMA_VERSION,
                "last_attempt_at": isoformat(attempted_at),
                "last_success_at": last_success,
                "refresh_status": "error",
                "error": {
                    "kind": error.__class__.__name__,
                    "message": str(error),
                },
            },
        )

    def _snapshot_state(self, snapshot: dict[str, Any] | None) -> str:
        if not snapshot or not snapshot.get("generated_at"):
            return "missing"
        if snapshot.get("schema_version") != SCHEMA_VERSION:
            return "stale"
        age = self._snapshot_age(snapshot)
        return "fresh" if age is not None and age <= self.config.stale_seconds else "stale"

    @staticmethod
    def _snapshot_age(snapshot: dict[str, Any] | None) -> float | None:
        if not snapshot or not snapshot.get("generated_at"):
            return None
        return max(
            0.0,
            (utc_now() - parse_time(snapshot["generated_at"])).total_seconds(),
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            with path.open(encoding="utf-8") as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as file:
                temporary_path = file.name
                json.dump(value, file, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            os.replace(temporary_path, path)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)
