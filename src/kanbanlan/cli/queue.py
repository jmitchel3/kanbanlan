"""Queue reads: ensure, refresh, status, snapshot, path, next, overlap, reconcile."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any

from kanbanlan.cli.cards import _local_view
from kanbanlan.cli.context import _context
from kanbanlan.cli.output import _emit_result, _rate_limit_text
from kanbanlan.domain import request_label
from kanbanlan.providers import CoordinationProvider
from kanbanlan.snapshot import READ_FROM_ISSUES, SCOPE_PROJECT, CacheStore, read_from, utc_now
from kanbanlan.ui import (
    field,
    heading,
    priority_value,
    section,
    status,
    status_value,
    success,
    warning,
)
from kanbanlan.workflow import apply_reconciliation, format_drift, plan_reconciliation, read_board


def _cmd_ensure(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    with status("Ensuring the shared board snapshot is fresh"):
        snapshot = store.ensure(provider)
    inspection = store.inspect()
    if _emit_result(
        args,
        {
            "snapshot_path": str(store.snapshot_path),
            "generated_at": snapshot["generated_at"],
            "snapshot_state": inspection["snapshot_state"],
            "refresh_status": inspection["refresh_status"],
            "rate_limit": snapshot.get("rate_limit"),
        },
    ):
        return 0
    _warn_stale_service(store, snapshot, inspection)
    print(store.snapshot_path)
    return 0


def _warn_stale_service(
    store: CacheStore,
    snapshot: dict[str, Any],
    inspection: dict[str, Any],
) -> None:
    """Say why ensure kept a stale snapshot instead of refreshing it."""

    if inspection["snapshot_state"] == "fresh":
        return
    deferral = store.rate_limit_deferral(snapshot, graphql=read_from(snapshot) != READ_FROM_ISSUES)
    if deferral:
        reason = (
            "GitHub rate-limit cooldown"
            if deferral.get("reason") == "cooldown"
            else f"{deferral['remaining']} GraphQL points remaining is below "
            f"the floor of {store.config.rate_limit_floor}"
        )
        warning(
            f"refresh deferred: {reason}; serving the cached snapshot "
            f"until the quota resets at {deferral['reset_at']}"
        )
    elif inspection.get("refresh_status") == "throttled":
        warning(
            "GitHub rate limited the refresh; serving the last good snapshot from "
            f"{inspection.get('last_success_at') or snapshot.get('generated_at')}"
        )


def _cmd_refresh(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    if getattr(args, "project_scope", False):
        snapshot = _project_snapshot(provider, "Refreshing the project-scope snapshot", store)
        if _emit_result(args, {"generated_at": snapshot["generated_at"], "scope": "project"}):
            return 0
        print(f"refreshed {store.project_snapshot_path} at {snapshot['generated_at']}")
        return 0
    # The queue read answers ensure, next, and status, and costs no GraphQL
    # points while nothing changed; reconcile does its own Project read.
    with status("Refreshing the shared board snapshot"):
        snapshot = store.refresh(provider)
    if _emit_result(
        args,
        {"snapshot_path": str(store.snapshot_path), "generated_at": snapshot["generated_at"]},
    ):
        return 0
    print(f"refreshed {store.snapshot_path} at {snapshot['generated_at']}")
    return 0


def _project_snapshot(
    provider: CoordinationProvider,
    label: str,
    store: CacheStore | None = None,
) -> dict[str, Any]:
    """Read the whole Project live without disturbing the repository cache.

    The shared cache is repository-scoped by contract, so a project-scoped read
    never touches it; given a store, the read is kept in its separate
    project-scope copy for later local reads. Every project scope command is
    read-only.
    """

    if not provider.capabilities.project_scope:
        raise RuntimeError(
            f"canonical home {provider.provider_name!r} does not support project scope"
        )
    with status(label):
        snapshot = provider.snapshot(generated_at=utc_now(), scope=SCOPE_PROJECT)
    if store is not None:
        try:
            store.write_project_snapshot(snapshot)
        except OSError:
            pass
    return snapshot


def _project_view(
    provider: CoordinationProvider,
    store: CacheStore,
    label: str,
) -> dict[str, Any]:
    """Read the project-scope view live, keeping a copy for later local reads."""

    if not provider.capabilities.project_scope:
        return _project_snapshot(provider, label)
    return _project_snapshot(provider, label, store)


def _project_source(snapshot: dict[str, Any]) -> dict[str, Any]:
    return snapshot.get("source", {})


def _warn_linkage_problems(snapshot: dict[str, Any]) -> None:
    """Report pull requests that named a request too ambiguously to link."""

    for problem in snapshot.get("linkage_problems", []):
        warning(
            f"{problem['pull_request']} was not linked ({problem['kind']}): {problem['detail']}"
        )


def _linkage_problems_for(snapshot: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    kanbanlan_id = item.get("kanbanlan_id")
    if not kanbanlan_id:
        return []
    return [
        problem
        for problem in snapshot.get("linkage_problems", [])
        if kanbanlan_id in problem.get("kanbanlan_ids", [])
    ]


def _warn_unavailable_repositories(snapshot: dict[str, Any]) -> None:
    for entry in _project_source(snapshot).get("unavailable_repositories", []):
        warning(f"peer repository {entry['repository']} could not be read: {entry['error']}")


def _repository_status_counts(snapshot: dict[str, Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for item in snapshot.get("items", []):
        repository = item.get("repository") or "unknown"
        name = item.get("status") or "Unspecified"
        counts.setdefault(repository, {})
        counts[repository][name] = counts[repository].get(name, 0) + 1
    return {key: dict(sorted(value.items())) for key, value in sorted(counts.items())}


def _cmd_status(args: argparse.Namespace) -> int:
    if getattr(args, "project_scope", False):
        return _cmd_status_project(args)
    _, _, _, store = _context(args)
    inspection = store.inspect()
    if _emit_result(args, inspection):
        return 0
    heading("Kanbanlan status")
    field("Snapshot", status_value(inspection["snapshot_state"]))
    if inspection["generated_at"]:
        field(
            "Generated",
            f"{inspection['generated_at']} ({inspection['age_seconds']:.0f}s ago)",
        )
    counts = inspection["status_counts"]
    board = ", ".join(f"{status_value(name)}={count}" for name, count in counts.items()) or "empty"
    field("Board", board)
    next_ready = inspection["next_ready"]
    if next_ready:
        field("Next", f"{request_label(next_ready)} {next_ready['title']}")
    rate = inspection.get("rate_limit") or {}
    if rate.get("remaining") is not None:
        field("Rate limit", _rate_limit_text(rate))
    if inspection.get("refresh_status") == "throttled":
        warning("the last refresh was rate limited; the snapshot may lag the live board")
    if inspection["error"]:
        warning(f"Last refresh: {inspection['error']['kind']}: {inspection['error']['message']}")
    return 0


def _cmd_status_project(args: argparse.Namespace) -> int:
    _, config, provider, _ = _context(args)
    snapshot = _project_snapshot(provider, "Reading every repository on the Project")
    source = _project_source(snapshot)
    per_repository = _repository_status_counts(snapshot)
    result = {
        "scope": source.get("scope"),
        "repository": config.repository,
        "repositories": source.get("repositories", []),
        "unavailable_repositories": source.get("unavailable_repositories", []),
        "generated_at": snapshot.get("generated_at"),
        "project": snapshot.get("project"),
        "status_counts": snapshot.get("status_counts", {}),
        "repository_status_counts": per_repository,
    }
    if _emit_result(args, result):
        return 0
    heading("Kanbanlan status (project scope)")
    project = snapshot.get("project", {})
    field("Project", f"{project.get('title')} (#{project.get('number')})")
    field("Generated", snapshot.get("generated_at"))
    counts = snapshot.get("status_counts", {})
    board = ", ".join(f"{status_value(name)}={count}" for name, count in counts.items()) or "empty"
    field("Board", board)
    for repository, values in per_repository.items():
        marker = repository
        if repository == config.repository:
            marker = f"{repository} (this repository)"
        summary = ", ".join(f"{status_value(name)}={count}" for name, count in values.items())
        field(marker, summary or "empty")
    _warn_unavailable_repositories(snapshot)
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    if getattr(args, "project_scope", False):
        _, _, provider, _ = _context(args)
        snapshot = _project_snapshot(provider, "Reading every repository on the Project")
    else:
        _, _, _, store = _context(args)
        snapshot = store.snapshot()
        if snapshot is None:
            raise RuntimeError("no snapshot is available; run 'kanbanlan ensure'")
    json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_overlap(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    snapshot = _project_view(provider, store, "Checking every open card and pull request")
    source = _project_source(snapshot)
    requests = [
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "provider_ref": item.get("provider_ref"),
            "display_id": item.get("display_id"),
            "repository": item.get("repository"),
            "title": item.get("title"),
            "status": item.get("status"),
            "priority": item.get("priority"),
            "url": item.get("canonical_url") or item.get("url"),
            "active_claim": item.get("active_claim"),
            "linked_open_pull_requests": item.get("linked_open_pull_requests", []),
        }
        for item in snapshot.get("items", [])
        if item.get("type") == "ISSUE" and item.get("state") == "OPEN"
    ]
    linked_urls = {
        pull_request["url"]
        for request in requests
        for pull_request in request["linked_open_pull_requests"]
    }
    unlinked = [
        pull_request
        for pull_request in snapshot.get("open_pull_requests", [])
        if pull_request["url"] not in linked_urls
    ]
    result = {
        "scope": source.get("scope"),
        "repository": config.repository,
        "repositories": source.get("repositories", []),
        "unavailable_repositories": source.get("unavailable_repositories", []),
        "generated_at": snapshot.get("generated_at"),
        "project": snapshot.get("project"),
        "open_requests": requests,
        "unlinked_open_pull_requests": unlinked,
        "linkage_problems": snapshot.get("linkage_problems", []),
    }
    if _emit_result(args, result):
        return 0
    heading("Project overlap check")
    project = snapshot.get("project", {})
    field("Project", f"{project.get('title')} (#{project.get('number')})")
    field("Repositories", ", ".join(source.get("repositories", [])) or "none")
    _warn_unavailable_repositories(snapshot)
    _warn_linkage_problems(snapshot)
    if not requests:
        print("No open requests are on the Project.")
    for request in requests:
        section(
            f"{request['provider_ref']} [{status_value(request.get('status') or 'Unspecified')}] "
            f"{request['title']}"
        )
        if request["kanbanlan_id"]:
            field("Kanbanlan", request["kanbanlan_id"])
        claim = request.get("active_claim")
        if claim:
            owner = claim.get("session") or claim.get("author") or "unknown session"
            field("Claim", f"{owner} at {claim['claimed_at']}")
            if claim.get("touchpoints"):
                field("Touchpoints", claim["touchpoints"])
        for pull_request in request["linked_open_pull_requests"]:
            routes = ", ".join(pull_request.get("linked_by", [])) or "link"
            field(
                "Pull request",
                f"{pull_request['provider_ref']} {pull_request['url']} ({routes})",
            )
    if unlinked:
        section("Open pull requests with no linked request")
        for pull_request in unlinked:
            field(pull_request["provider_ref"], pull_request["url"])
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    _, _, _, store = _context(args)
    if _emit_result(args, {"snapshot_path": str(store.snapshot_path)}):
        return 0
    print(store.snapshot_path)
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    root, _, provider, store = _context(args)
    snapshot = _local_view(root, store, provider, label="Finding the next unblocked Ready card")
    item = snapshot.get("next_ready")
    if not item:
        if _emit_result(args, {"request": None}):
            return 0
        print("No unblocked Ready card is available.")
        return 0
    if _emit_result(args, {"request": item}):
        return 0
    priority = item.get("priority") or "unprioritized"
    print(f"{request_label(item)} [{priority_value(priority)}] {item['title']}")
    print(item["url"])
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    with status("Loading current Project and issue state"):
        snapshot, open_issues = read_board(store, provider)
    drift = plan_reconciliation(snapshot, open_issues)
    if not args.json_output:
        _warn_linkage_problems(snapshot)
    if not drift:
        if _emit_result(
            args,
            {
                "applied": False,
                "drift": [],
                "remaining": [],
                "linkage_problems": snapshot.get("linkage_problems", []),
            },
        ):
            return 0
        success("GitHub Issues and Project Status are reconciled.")
        return 0
    drift_payload = [asdict(value) for value in drift]
    if args.json_output and not args.apply:
        _emit_result(
            args,
            {
                "applied": False,
                "drift": drift_payload,
                "remaining": drift_payload,
                "linkage_problems": snapshot.get("linkage_problems", []),
            },
        )
        return 2
    for value in drift:
        print(format_drift(value))
    if not args.apply:
        warning(f"{len(drift)} difference(s); rerun with --apply to repair them.")
        return 2
    with status(f"Applying {len(drift)} reconciliation repair(s)"):
        remaining, _ = apply_reconciliation(provider, store, snapshot, open_issues)
    if args.json_output:
        _emit_result(
            args,
            {
                "applied": True,
                "drift": drift_payload,
                "remaining": [asdict(value) for value in remaining],
                "linkage_problems": snapshot.get("linkage_problems", []),
            },
        )
        return 1 if remaining else 0
    if remaining:
        for value in remaining:
            print(f"remaining: {format_drift(value)}")
        return 1
    success("Reconciliation applied and verified.")
    return 0
