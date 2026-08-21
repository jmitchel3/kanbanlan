from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kanbanlan.config import Config
from kanbanlan.identity import extract_kanbanlan_id, find_kanbanlan_ids
from kanbanlan.locks import FileLock
from kanbanlan.runner import RateLimitError
from kanbanlan.sessions import session_history

SCHEMA_VERSION = 3
SCOPE_REPOSITORY = "repository"
SCOPE_PROJECT = "project"
SCOPES = (SCOPE_REPOSITORY, SCOPE_PROJECT)
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


def qualified_reference(repository: str | None, number: int | None) -> str:
    """Return the repository-qualified provider reference for one issue or PR."""

    if repository is None or number is None:
        return f"#{number}" if number is not None else "unknown"
    return f"github:{repository}#{number}"


def display_reference(repository: str | None, number: int | None, local: str | None) -> str:
    """Return a short reference that stays unambiguous outside ``local``."""

    if number is None:
        return "unknown"
    if repository is None or repository == local:
        return f"#{number}"
    return f"{repository}#{number}"


def _normalize_pull_request(
    pull_request: dict[str, Any],
    fallback_repository: str,
) -> dict[str, Any]:
    repository = (pull_request.get("repository") or {}).get("nameWithOwner") or fallback_repository
    closing_references = []
    for issue in pull_request.get("closingIssuesReferences", {}).get("nodes", []):
        issue_repository = (issue.get("repository") or {}).get("nameWithOwner") or repository
        closing_references.append(
            {
                "repository": issue_repository,
                "number": issue["number"],
                "url": issue.get("url"),
                "provider_ref": qualified_reference(issue_repository, issue["number"]),
            }
        )
    closing_numbers = [
        value["number"] for value in closing_references if value["repository"] == repository
    ]
    return {
        "number": pull_request["number"],
        "repository": repository,
        "provider": "github",
        "provider_ref": qualified_reference(repository, pull_request["number"]),
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
        "closing_issue_references": sorted(
            closing_references,
            key=lambda value: (value["repository"], value["number"]),
        ),
        "declared_kanbanlan_id": extract_kanbanlan_id(pull_request.get("body")),
        "kanbanlan_ids": find_kanbanlan_ids(pull_request.get("body")),
    }


def _merge_pull_requests(entries: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    """Collapse one request's linkage entries into stable pull request records.

    A pull request can reach the same request through more than one route, so
    ``linked_by`` reports every route that justified the link.
    """

    by_url: dict[str, dict[str, Any]] = {}
    reasons: dict[str, set[str]] = {}
    for pull_request, reason in entries:
        url = pull_request["url"]
        by_url[url] = pull_request
        reasons.setdefault(url, set()).add(reason)
    merged = [
        {**pull_request, "linked_by": sorted(reasons[url])} for url, pull_request in by_url.items()
    ]
    return sorted(
        merged,
        key=lambda value: (value.get("repository") or "", value.get("number", 0)),
    )


def _project_identity_owners(project: dict[str, Any]) -> dict[str, list[tuple[str, int]]]:
    """Map each declared Kanbanlan ID to the Project requests that carry it.

    Every repository on the Project is considered, whatever the read scope,
    because a duplicated identity in a peer repository makes an identity link
    ambiguous here too.
    """

    owners: dict[str, list[tuple[str, int]]] = {}
    for raw_item in project.get("items", []):
        if raw_item.get("isArchived") or raw_item.get("type") != "ISSUE":
            continue
        content = raw_item.get("content") or {}
        repository = (content.get("repository") or {}).get("nameWithOwner")
        number = content.get("number")
        kanbanlan_id = extract_kanbanlan_id(content.get("body"))
        if kanbanlan_id and repository and number is not None:
            owners.setdefault(kanbanlan_id, []).append((repository, number))
    return owners


def _link_pull_requests(
    pull_requests: list[dict[str, Any]],
    identity_owners: dict[str, list[tuple[str, int]]],
) -> tuple[dict[tuple[str, int], list[tuple[dict[str, Any], str]]], list[dict[str, Any]]]:
    """Associate open pull requests with the requests they deliver.

    Two routes are explicit enough to cross a repository boundary. A closing
    reference is one: GitHub only resolves a closing reference to another
    repository when the author qualified it, so a bare ``Closes #34`` stays in
    the pull request's own repository without any check here. A declared
    Kanbanlan ID is the other.

    Anything ambiguous is reported and linked to nothing.
    """

    linked: dict[tuple[str, int], list[tuple[dict[str, Any], str]]] = {}
    problems: list[dict[str, Any]] = []
    for pull_request in pull_requests:
        targets: list[tuple[tuple[str, int], str]] = [
            ((reference["repository"], reference["number"]), "closing_reference")
            for reference in pull_request["closing_issue_references"]
        ]
        declared = pull_request["declared_kanbanlan_id"]
        mentioned = pull_request["kanbanlan_ids"]
        candidate = declared or (mentioned[0] if len(mentioned) == 1 else None)
        if candidate is None and len(mentioned) > 1:
            problems.append(
                {
                    "kind": "conflicting_kanbanlan_ids",
                    "pull_request": pull_request["provider_ref"],
                    "repository": pull_request["repository"],
                    "url": pull_request["url"],
                    "kanbanlan_ids": mentioned,
                    "detail": ("pull request names more than one Kanbanlan ID and declares none"),
                }
            )
        elif candidate:
            owners = identity_owners.get(candidate, [])
            if len(owners) > 1:
                problems.append(
                    {
                        "kind": "duplicate_kanbanlan_id",
                        "pull_request": pull_request["provider_ref"],
                        "repository": pull_request["repository"],
                        "url": pull_request["url"],
                        "kanbanlan_ids": [candidate],
                        "detail": (
                            f"Kanbanlan ID {candidate} is carried by "
                            + ", ".join(
                                qualified_reference(repository, number)
                                for repository, number in sorted(owners)
                            )
                        ),
                    }
                )
            elif owners:
                targets.append((owners[0], "kanbanlan_id"))
        for key, reason in targets:
            linked.setdefault(key, []).append((pull_request, reason))
    return linked, sorted(problems, key=lambda value: value["pull_request"])


def build_snapshot(
    config: Config,
    project: dict[str, Any],
    raw_pull_requests: list[dict[str, Any]],
    rate_limit: dict[str, Any],
    generated_at: datetime | None = None,
    *,
    scope: str = SCOPE_REPOSITORY,
    unavailable_repositories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize one Project read into the stable snapshot document.

    ``scope`` selects how much of a shared Project the snapshot keeps.
    ``repository`` scope, the default every lifecycle command uses, keeps only
    content owned by ``config.repository``. ``project`` scope keeps every
    Project item so an agent can inspect peer repositories before claiming
    work. Queue selection stays repository-local in both scopes.
    """

    if scope not in SCOPES:
        raise ValueError(f"unsupported snapshot scope: {scope!r}")
    generated_at = generated_at or utc_now()
    pull_requests = [
        _normalize_pull_request(value, config.repository) for value in raw_pull_requests
    ]
    linked_pull_requests, linkage_problems = _link_pull_requests(
        pull_requests,
        _project_identity_owners(project),
    )

    items: list[dict[str, Any]] = []
    for raw_item in project.get("items", []):
        if raw_item.get("isArchived"):
            continue
        content = raw_item.get("content") or {}
        item_type = raw_item.get("type", "UNKNOWN")
        content_repository = (content.get("repository") or {}).get("nameWithOwner")
        if (
            scope == SCOPE_REPOSITORY
            and item_type in {"ISSUE", "PULL_REQUEST"}
            and content_repository != config.repository
        ):
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
            "repository": content_repository,
        }
        if item_type == "ISSUE":
            comments = content.get("comments", {})
            comment_nodes = comments.get("nodes", [])
            labels = _labels(content)
            label_names = [label["name"] for label in labels]
            priority = next(
                (label for label in label_names if label in PRIORITY_ORDER),
                None,
            )
            number = content["number"]
            kanbanlan_id = extract_kanbanlan_id(content.get("body"))
            provider_ref = qualified_reference(content_repository, number)
            normalized.update(
                {
                    "kanbanlan_id": kanbanlan_id,
                    "provider": "github",
                    "provider_id": content.get("id"),
                    "provider_ref": provider_ref,
                    "display_id": display_reference(content_repository, number, config.repository),
                    "canonical_url": content.get("url"),
                    "content_id": content.get("id"),
                    "number": number,
                    "repository": content_repository,
                    "state": content.get("state"),
                    "state_reason": content.get("stateReason"),
                    "closed_at": content.get("closedAt"),
                    "labels": labels,
                    "milestone": (content.get("milestone") or {}).get("title"),
                    "priority": priority,
                    "assignees": sorted(
                        assignee["login"]
                        for assignee in content.get("assignees", {}).get("nodes", [])
                    ),
                    "active_claim": (
                        active_claim(comment_nodes) if content.get("state") == "OPEN" else None
                    ),
                    "session_history": session_history(comment_nodes),
                    "session_history_truncated": int(
                        comments.get("totalCount") or len(comment_nodes)
                    )
                    > len(comment_nodes),
                    "linked_open_pull_requests": _merge_pull_requests(
                        linked_pull_requests.get((content_repository, number), [])
                    ),
                }
            )
        elif item_type == "PULL_REQUEST":
            normalized.update(
                {
                    "content_id": content.get("id"),
                    "number": content.get("number"),
                    "provider": "github",
                    "provider_ref": qualified_reference(content_repository, content.get("number")),
                    "display_id": display_reference(
                        content_repository, content.get("number"), config.repository
                    ),
                    "state": content.get("state"),
                    "is_draft": content.get("isDraft", False),
                    "merged_at": content.get("mergedAt"),
                }
            )
        items.append(normalized)

    # A peer repository's pull request stays in the document only while it
    # delivers a request kept by this scope. Repository scope therefore never
    # lists unrelated peer work, but never hides the delivery of its own card.
    relevant_pull_request_urls = {
        pull_request["url"]
        for item in items
        if item["type"] == "ISSUE"
        for pull_request in item["linked_open_pull_requests"]
    }
    pull_requests = [
        pull_request
        for pull_request in pull_requests
        if scope == SCOPE_PROJECT
        or pull_request["repository"] == config.repository
        or pull_request["url"] in relevant_pull_request_urls
    ]
    reported_urls = {pull_request["url"] for pull_request in pull_requests}
    linkage_problems = [problem for problem in linkage_problems if problem["url"] in reported_urls]

    status_counts: dict[str, int] = {}
    for item in items:
        status = item.get("status") or "Unspecified"
        status_counts[status] = status_counts.get(status, 0) + 1

    # Queue selection stays repository-local even under project scope so a
    # shared Project never hands one repository another repository's work.
    ready = [
        item
        for item in items
        if item["type"] == "ISSUE"
        and item.get("repository") in (config.repository, None)
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
            "scope": scope,
            "repository": config.repository,
            "repositories": sorted(
                {
                    value
                    for value in (
                        [config.repository]
                        + [item.get("repository") for item in items]
                        + [value["repository"] for value in pull_requests]
                    )
                    if value
                }
            ),
            "unavailable_repositories": sorted(
                unavailable_repositories or [],
                key=lambda value: value.get("repository", ""),
            ),
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
        "linkage_problems": linkage_problems,
        "rate_limit": rate_limit,
    }


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
            return self._refresh_locked(client)

    def _refresh_locked(self, client: Any) -> dict[str, Any]:
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
        except RateLimitError as exc:
            self._write_failure_health(attempted_at, exc, refresh_status="throttled")
            raise
        except Exception as exc:
            self._write_failure_health(attempted_at, exc)
            raise

    def ensure(self, client: Any) -> dict[str, Any]:
        """Return a fresh snapshot, or the best usable one under rate pressure.

        Freshness is re-checked after the refresh lock is acquired, because a
        concurrent session may have completed the very fetch this one queued
        for; repeating it would spend the same GraphQL points for nothing. A
        refresh refused for quota reasons falls back to the last good
        snapshot, so only a missing snapshot makes rate limiting fatal.
        """

        snapshot = self.snapshot()
        if self._snapshot_state(snapshot) == "fresh":
            assert snapshot is not None
            return snapshot
        if self.rate_limit_deferral(snapshot):
            assert snapshot is not None
            return snapshot
        self._prepare_directory()
        with FileLock(self.lock_path):
            snapshot = self.snapshot()
            if self._snapshot_state(snapshot) == "fresh":
                assert snapshot is not None
                return snapshot
            try:
                return self._refresh_locked(client)
            except RateLimitError:
                if self._usable(snapshot):
                    assert snapshot is not None
                    return snapshot
                raise

    def rate_limit_deferral(self, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        """Explain why a refresh should wait, or return None to proceed.

        Spending the last points before the quota resets trades a slightly
        stale board for every other GitHub call failing outright, so a stale
        snapshot keeps serving while its recorded ``remaining`` sits below the
        configured floor and the reset is still ahead.
        """

        floor = self.config.rate_limit_floor
        if floor <= 0 or not self._usable(snapshot):
            return None
        assert snapshot is not None
        rate = snapshot.get("rate_limit") or {}
        remaining = rate.get("remaining")
        reset_at = rate.get("resetAt")
        if isinstance(remaining, bool) or not isinstance(remaining, int):
            return None
        if remaining >= floor or not isinstance(reset_at, str) or not reset_at:
            return None
        try:
            # TypeError covers a timezone-naive resetAt, which parses but
            # cannot be compared with an aware "now".
            if parse_time(reset_at) <= utc_now():
                return None
        except (ValueError, TypeError):
            return None
        return {"remaining": remaining, "reset_at": reset_at}

    def _usable(self, snapshot: dict[str, Any] | None) -> bool:
        # Serving stale is only safe when the current code understands the
        # document; an old-schema snapshot is as unusable as a missing one.
        return bool(
            snapshot
            and snapshot.get("generated_at")
            and snapshot.get("schema_version") == SCHEMA_VERSION
        )

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
            "rate_limit": snapshot.get("rate_limit") if snapshot else None,
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

    def _write_failure_health(
        self,
        attempted_at: datetime,
        error: Exception,
        *,
        refresh_status: str = "error",
    ) -> None:
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
                "refresh_status": refresh_status,
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
