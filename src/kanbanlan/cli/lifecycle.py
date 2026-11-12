"""Request lifecycle commands: capture, triage, claim, release, rehome, review, close, handoff."""

from __future__ import annotations

import argparse
import re
import shlex
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from kanbanlan.cli.cards import (
    _issue,
    _post_claim,
    _read_card,
    _record_card,
    _refresh_after_mutation,
    _set_card_status,
    _set_state,
    _verify_claim,
)
from kanbanlan.cli.context import _actor_session, _context, _record_session_activity
from kanbanlan.cli.output import _emit_result, _utc_timestamp
from kanbanlan.cli.queue import _linkage_problems_for, _project_snapshot
from kanbanlan.config import Config, normalize_repository_target
from kanbanlan.domain import request_label
from kanbanlan.identity import attach_kanbanlan_id, new_kanbanlan_id, normalize_kanbanlan_id
from kanbanlan.providers import CoordinationProvider
from kanbanlan.rehome import format_plan, plan_rehome, rehome_result
from kanbanlan.runner import CommandError, Runner
from kanbanlan.sessions import AgentSession
from kanbanlan.snapshot import SCOPE_PROJECT, CacheStore, qualified_reference, utc_now
from kanbanlan.ui import field, heading, status, success, warning
from kanbanlan.workflow import STATUS_TO_LABEL


def _capture_target(
    args: argparse.Namespace,
    config: Config,
    provider: CoordinationProvider,
) -> tuple[str, dict[str, Any] | None]:
    """Resolve where a new request belongs, without ever guessing.

    A target is only ever the configured repository or one the caller named.
    Nothing is inferred from the title or body.
    """

    requested = getattr(args, "repository", None)
    if not requested:
        return config.repository, None
    if not provider.capabilities.repository_routing:
        raise RuntimeError(
            f"canonical home {provider.provider_name!r} does not support repository routing"
        )
    target = normalize_repository_target(requested, hostname=config.hostname)
    if target == config.repository:
        return config.repository, None
    with status(f"Preparing {target} for capture"):
        preparation = provider.prepare_capture_target(target)
    return preparation["repository"], preparation


def _place_captured_request(
    provider: CoordinationProvider,
    store: CacheStore,
    url: str,
    kanbanlan_id: str,
    target: str,
    title: str,
) -> dict[str, Any]:
    """Put one freshly created request in the Project's Inbox.

    Only this card is written, so nothing here reads the board or waits on
    the refresh lock: the Project item id comes back from the add, and the
    Status field comes from the cached snapshot, or from a metadata-only read
    when the cache is missing or its field ids have gone stale. The issue was
    created with ``status:intake`` already.
    """

    added = provider.add_to_projection(url)
    item_id = (added or {}).get("id")
    if not item_id:
        raise RuntimeError("GitHub did not report the new Project item")
    _set_card_status(provider, store, item_id, "Inbox")
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    return {
        "kanbanlan_id": kanbanlan_id,
        "number": number,
        "repository": target,
        "provider_ref": qualified_reference(target, number),
        "canonical_url": url,
        "url": url,
        "title": title,
        "state": "OPEN",
        "type": "ISSUE",
        "status": "Inbox",
        "project_item_id": item_id,
    }


def _cmd_capture(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    target, preparation = _capture_target(args, config, provider)
    kanbanlan_id = new_kanbanlan_id()
    if getattr(args, "kanbanlan_id", None):
        kanbanlan_id = normalize_kanbanlan_id(args.kanbanlan_id)
        if not kanbanlan_id:
            raise RuntimeError(f"invalid Kanbanlan ID {args.kanbanlan_id!r}")
        # A queued capture replayed after an unknown outcome must not open
        # a second issue for the same request.
        existing = _existing_request(provider, store, kanbanlan_id, target, config)
        if existing is not None:
            if existing.get("project_item_id") is None and _still_intake(existing):
                # The lost attempt may have died between creating the issue
                # and placing it; adding is idempotent, so finish the job.
                with status("Adding issue to the configured Project Inbox"):
                    existing = {
                        **existing,
                        **_place_captured_request(
                            provider,
                            store,
                            existing["url"],
                            kanbanlan_id,
                            target,
                            existing.get("title") or args.title,
                        ),
                    }
            return _emit_capture(args, existing, kanbanlan_id, target, config, None, actor)
    body = args.body or (
        "## Outcome\n\n"
        "<!-- Describe the independently reviewable result. -->\n\n"
        "## Acceptance criteria\n\n- [ ] "
    )
    body = attach_kanbanlan_id(body, kanbanlan_id)
    with status(f"Creating Inbox issue in {target}"):
        url = provider.create_request(args.title, body, args.priority, repository=target)
    try:
        with status("Adding issue to the configured Project Inbox"):
            item = _place_captured_request(provider, store, url, kanbanlan_id, target, args.title)
    except (CommandError, RuntimeError) as exc:
        raise RuntimeError(
            f"the issue was created at {url}, but Project setup failed: {exc}. "
            f"Do not run capture again; repair the existing request with "
            f"{shlex.join(['kanbanlan', 'reconcile', '--apply'])} in {target}"
        ) from exc
    _record_session_activity(
        config=config,
        provider=provider,
        reference=item["number"],
        action="capture",
        from_status=None,
        to_status="Inbox",
        actor=actor,
        repository=target,
    )
    if target == config.repository:
        _refresh_after_mutation(root, store, provider)
    return _emit_capture(args, item, kanbanlan_id, target, config, preparation, actor)


def _still_intake(item: dict[str, Any]) -> bool:
    names = {label.get("name") for label in item.get("labels") or []}
    return item.get("state") == "OPEN" and "status:intake" in names


def _existing_request(
    provider: CoordinationProvider,
    store: CacheStore,
    kanbanlan_id: str,
    target: str,
    config: Config,
) -> dict[str, Any] | None:
    """Find a request a replayed capture may already have created.

    The cached board answers first; otherwise one issue search in the target
    repository settles it, so a replay never waits on a full board read.
    """

    snapshot = store.snapshot()
    if snapshot and target == config.repository:
        try:
            cached = _issue(snapshot, kanbanlan_id)
        except RuntimeError:
            cached = None
        if cached is not None and cached.get("number") is not None:
            return cached
    return provider.find_request(kanbanlan_id, repository=target)


def _emit_capture(
    args: argparse.Namespace,
    item: dict[str, Any],
    kanbanlan_id: str,
    target: str,
    config: Config,
    preparation: dict[str, Any] | None,
    actor: AgentSession | None,
) -> int:
    url = item.get("url")
    result = {
        "kanbanlan_id": kanbanlan_id,
        "repository": target,
        "provider_ref": item.get("provider_ref") if item.get("number") is not None else None,
        "canonical_url": item.get("canonical_url") or url,
        "url": url,
        "routed": target != config.repository,
        "project_link": preparation,
        "actor_session": actor.to_dict() if actor else None,
    }
    if _emit_result(args, result):
        return 0
    print(f"Kanbanlan: {kanbanlan_id}")
    if target != config.repository:
        print(f"repository: {target}")
    print(url)
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking request {args.issue}"):
        card = _read_card(provider, store, config, args.issue)
    item = _issue(card, args.issue)
    number = item["number"]
    label = request_label(item)
    if item.get("state") != "OPEN":
        raise RuntimeError(f"request {label} is not open")
    if item.get("status") != "Inbox":
        raise RuntimeError(f"request {label} is {item.get('status')!r}, not Inbox")
    with status(f"Moving {label} to Ready"):
        _set_state(provider, store, item, "status:ready", "Ready")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="triage",
            from_status="Inbox",
            to_status="Ready",
            actor=actor,
        )
        _record_card(store, item, "status:ready", "Ready")
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "status": "Ready",
        "actor_session": actor.to_dict() if actor else None,
    }
    if _emit_result(args, result):
        return 0
    success(f"Moved {label} to Ready")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    # 'start' hands over the card it already checked in the local view; the
    # read after the claim comment then checks it live instead of a read
    # before it, and a claim it refuses is released.
    known = getattr(args, "known_card", None)
    if known is None:
        with status(f"Checking request {args.issue}"):
            card = _read_card(provider, store, config, args.issue)
        item = _issue(card, args.issue)
    else:
        item = known
    number = item["number"]
    label = request_label(item)
    _require_claimable(item, label)

    session = args.session or (actor.reference if actor else None)
    session = session or f"kanbanlan-{uuid.uuid4().hex[:8]}"
    branch, worktree = _claim_checkout(args, root, config, item)
    timestamp = _utc_timestamp()
    body = (
        f"CLAIM: {timestamp}\n"
        f"Kanbanlan: {item.get('kanbanlan_id') or 'unassigned'}\n"
        f"Session: {session}\n"
        f"Branch: {branch}\n"
        f"Worktree: {worktree}\n"
        f"Touchpoints: {args.touchpoints}"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        # Only the fetch runs while GitHub settles the claim. The worktree is
        # added once the claim holds: two sessions on one machine racing for
        # one card compute the same path, and the loser must never take it.
        fetch = None if args.no_worktree else pool.submit(_fetch_default_branch, root, config)
        posted_claim = False
        try:
            with status(f"Posting claim for {label}"):
                posted_claim = True
                posted = _post_claim(provider, item, body)
                claimed_item = _verify_claim(
                    provider, store, config, number, session, posted=posted
                )
            reason, failure = _claim_failure(args, claimed_item, label, session, known)
            if failure is None and fetch is not None:
                try:
                    _create_worktree(root, config, branch, Path(worktree), fetch=not fetch.result())
                except Exception as exc:  # noqa: BLE001 - reported once released
                    reason, failure = "worktree creation failed", exc
        except BaseException:
            if posted_claim:
                # A claim this command cannot settle must not stay active.
                _release_quietly(provider, number, session, "claim interrupted")
            raise
        if failure is not None:
            provider.comment_request(
                number,
                f"RELEASED: {_utc_timestamp()} — {reason}\nSession: {session}",
            )
            raise failure

    with status("Moving request to In progress"), ThreadPoolExecutor(max_workers=1) as pool:
        activity = pool.submit(
            _record_session_activity,
            config=config,
            provider=provider,
            reference=number,
            action="claim",
            from_status="Ready",
            to_status="In progress",
            actor=actor,
            owner_session=session,
        )
        _set_state(provider, store, claimed_item, "status:in-progress", "In progress")
        activity.result()
    _record_card(store, claimed_item, "status:in-progress", "In progress")
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "provider_ref": item.get("provider_ref"),
        "session": session,
        "actor_session": actor.to_dict() if actor else None,
        "branch": branch,
        "worktree": worktree,
    }
    if _emit_result(args, result):
        return 0
    success(f"Claimed {label} as {session}")
    print(f"branch: {branch}")
    print(f"worktree: {worktree}")
    return 0


def _require_claimable(item: dict[str, Any], label: str) -> None:
    if item.get("state") != "OPEN":
        raise RuntimeError(f"request {label} is not open")
    if item.get("status") != "Ready":
        raise RuntimeError(f"request {label} is {item.get('status')!r}, not Ready")
    if item.get("active_claim"):
        owner = item["active_claim"].get("session") or "another session"
        raise RuntimeError(f"request {label} already has an active claim by {owner}")


def _claim_failure(
    args: argparse.Namespace,
    claimed_item: dict[str, Any],
    label: str,
    session: str,
    known: dict[str, Any] | None,
) -> tuple[str, Exception | None]:
    """Decide whether a posted claim holds, before anything moves the card."""

    claimed = claimed_item.get("active_claim") or {}
    if claimed.get("session") != session:
        owner = claimed.get("session") or "another session"
        lost = RuntimeError(f"request {label} was claimed first by {owner}")
        return "concurrent claim lost", lost
    if known is not None:
        # The card came from the local view; this is its first live check.
        try:
            _require_claimable({**claimed_item, "active_claim": None}, label)
        except RuntimeError as exc:
            return "request was not claimable", exc
    gate = getattr(args, "claim_gate", None)
    if gate is not None:
        try:
            gate()
        except RuntimeError as exc:
            return "claim refused", exc
    return "", None


def _fetch_default_branch(root: Path, config: Config) -> bool:
    """Fetch the default branch for a new worktree; report whether it worked."""

    result = Runner(root).run(["git", "fetch", "origin", config.default_branch], check=False)
    return result.returncode == 0


def _release_quietly(
    provider: CoordinationProvider,
    number: int,
    session: str,
    reason: str,
) -> None:
    """Release a claim on the way out of a failure, never masking that failure."""

    try:
        provider.comment_request(
            number,
            f"RELEASED: {_utc_timestamp()} — {reason}\nSession: {session}",
        )
    except Exception:  # noqa: BLE001 - the original failure is the one to report
        warning(f"could not release the claim on #{number}; release it with 'kanbanlan drop'")


def _claim_checkout(
    args: argparse.Namespace,
    root: Path,
    config: Config,
    item: dict[str, Any],
) -> tuple[str, str]:
    runner = Runner(root)
    if args.no_worktree:
        branch = args.branch or runner.run(["git", "branch", "--show-current"]).stdout.strip()
        if not branch or branch == config.default_branch:
            raise RuntimeError("--no-worktree requires an existing non-default branch or --branch")
        return branch, str(Path(args.worktree).resolve() if args.worktree else root)

    slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")[:48]
    identity = (item.get("kanbanlan_id") or str(item["number"])).lower()
    branch = args.branch or f"work/{identity}-{slug or 'request'}"
    common = Path(
        runner.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
    ).resolve()
    default_worktree = common.parent / ".worktrees" / f"{identity}-{slug or 'request'}"
    worktree = Path(args.worktree).resolve() if args.worktree else default_worktree
    if worktree.exists():
        raise RuntimeError(f"worktree path already exists: {worktree}")
    return branch, str(worktree)


def _create_worktree(
    root: Path,
    config: Config,
    branch: str,
    worktree: Path,
    *,
    fetch: bool = True,
) -> None:
    runner = Runner(root)
    if fetch:
        runner.run(["git", "fetch", "origin", config.default_branch])
    branch_exists = (
        runner.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 0
    )
    args = ["git", "worktree", "add"]
    if branch_exists:
        args.extend([str(worktree), branch])
    else:
        args.extend(
            [
                "-b",
                branch,
                str(worktree),
                f"origin/{config.default_branch}",
            ]
        )
    runner.run(args)


def _cmd_release(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking active claim for request {args.issue}"):
        card = _read_card(provider, store, config, args.issue)
    item = _issue(card, args.issue)
    number = item["number"]
    label = request_label(item)
    claim = item.get("active_claim")
    if not claim:
        raise RuntimeError(f"request {label} has no active claim")
    session = claim.get("session") or "unknown"
    destination = "Blocked" if args.blocked else "Ready"
    with status(f"Releasing claim and moving request to {destination}"):
        provider.comment_request(
            number,
            (f"RELEASED: {_utc_timestamp()} — {args.reason}\nSession: {session}"),
        )
        if args.blocked:
            _set_state(provider, store, item, "status:blocked", "Blocked")
        else:
            _set_state(provider, store, item, "status:ready", "Ready")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="release",
            from_status="In progress",
            to_status=destination,
            actor=actor,
            owner_session=session,
        )
        _record_card(
            store,
            item,
            "status:blocked" if args.blocked else "status:ready",
            destination,
            active_claim=None,
        )
    if _emit_result(
        args,
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "session": session,
            "status": destination,
            "actor_session": actor.to_dict() if actor else None,
        },
    ):
        return 0
    success(f"Released {label} from {session}")
    return 0


def _cmd_rehome(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    if not provider.capabilities.request_rehoming:
        raise RuntimeError(
            f"canonical home {provider.provider_name!r} does not support rehoming a request"
        )
    target = normalize_repository_target(args.repository, hostname=config.hostname)
    # A request that belongs elsewhere may already live in a peer repository,
    # so it is resolved across the Project rather than locally.
    snapshot = _project_snapshot(provider, f"Checking request {args.issue}")
    item = _issue(snapshot, args.issue)
    with status(f"Inspecting {target}"):
        inspection = provider.inspect_repository_target(target)
    plan = plan_rehome(item, target, inspection)

    if not args.apply:
        if _emit_result(args, {"applied": False, **plan.to_dict()}):
            return 2 if plan.blocked else 0
        heading("Rehome plan")
        for line in format_plan(plan):
            print(line)
        if plan.blocked:
            warning(f"{len(plan.blockers)} blocker(s); resolve them before rerunning with --apply.")
            return 2
        warning("Nothing was changed; rerun with --apply to perform the move.")
        return 0

    if plan.blocked:
        detail = "; ".join(f"{value.kind}: {value.detail}" for value in plan.blockers)
        raise RuntimeError(f"request {plan.kanbanlan_id} cannot be rehomed ({detail})")

    with status(f"Preparing {target}"):
        provider.prepare_repository_target(target)
    with status(f"Transferring {plan.source_provider_ref} to {target}"):
        moved_url = provider.transfer_request(
            plan.source_number,
            target,
            repository=plan.source_repository,
        )
    try:
        with status("Reconciling the moved request"):
            moved = _reconcile_rehomed_request(provider, plan, moved_url, config)
    except (CommandError, RuntimeError) as exc:
        recovery = shlex.join(
            ["kanbanlan", "rehome", plan.kanbanlan_id, "--repository", target, "--apply"]
        )
        raise RuntimeError(
            f"the request moved to {moved_url}, but reconciling it failed: {exc}. "
            f"The request was not duplicated; rerun {recovery} to finish, or repair it with "
            f"{shlex.join(['kanbanlan', 'reconcile', '--apply'])} in {target}"
        ) from exc
    _record_session_activity(
        config=config,
        provider=provider,
        reference=moved["number"],
        action="rehome",
        from_status=plan.status,
        to_status=moved.get("status"),
        actor=actor,
        repository=target,
    )
    if plan.source_repository == config.repository or target == config.repository:
        _refresh_after_mutation(root, store, provider)
    result = rehome_result(plan, moved)
    result["actor_session"] = actor.to_dict() if actor else None
    if _emit_result(args, result):
        return 0
    success(f"Rehomed {plan.kanbanlan_id} to {target}")
    print(f"{plan.source_provider_ref} -> {result['target']['provider_ref']}")
    print(result["target"]["url"])
    for value in plan.dropped:
        warning(f"dropped {value['field']} {value['value']}: {value['reason']}")
    return 0


def _reconcile_rehomed_request(
    provider: CoordinationProvider,
    plan: Any,
    moved_url: str,
    config: Config,
) -> dict[str, Any]:
    """Restore the request's Project state after the transfer.

    Resolution is by Kanbanlan ID, which the transfer preserved, so this never
    creates a replacement request even when it runs more than once.
    """

    snapshot = provider.snapshot(generated_at=utc_now(), scope=SCOPE_PROJECT)
    try:
        item = _issue(snapshot, plan.kanbanlan_id)
    except RuntimeError:
        provider.add_to_projection(moved_url)
        snapshot = provider.snapshot(generated_at=utc_now(), scope=SCOPE_PROJECT)
        item = _issue(snapshot, plan.kanbanlan_id)
    if not item.get("project_item_id"):
        provider.add_to_projection(item.get("canonical_url") or moved_url)
        snapshot = provider.snapshot(generated_at=utc_now(), scope=SCOPE_PROJECT)
        item = _issue(snapshot, plan.kanbanlan_id)
    label = STATUS_TO_LABEL.get(plan.status or "")
    if label:
        provider.set_request_status(item["number"], label, repository=plan.target_repository)
    if plan.status:
        provider.set_projection_status(
            item["project_item_id"],
            snapshot["project"],
            plan.status,
        )
    return item


def _cmd_review(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking pull requests for request {args.issue}"):
        card = _read_card(provider, store, config, args.issue, pull_requests=True)
    item = _issue(card, args.issue)
    number = item["number"]
    label = request_label(item)
    linked = item.get("linked_open_pull_requests") or []
    if not linked:
        blocked = _linkage_problems_for(card, item)
        if blocked:
            detail = "; ".join(
                f"{problem['pull_request']} ({problem['kind']}): {problem['detail']}"
                for problem in blocked
            )
            raise RuntimeError(
                f"request {label} has no linked open pull request; "
                f"an ambiguous reference was not associated: {detail}"
            )
        raise RuntimeError(f"request {label} has no linked open pull request")
    with status("Moving request to In review"):
        _set_state(provider, store, item, "status:review", "In review")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="review",
            from_status=item.get("status"),
            to_status="In review",
            actor=actor,
            owner_session=(item.get("active_claim") or {}).get("session"),
        )
        _record_card(store, item, "status:review", "In review")
    if _emit_result(
        args,
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "status": "In review",
            "actor_session": actor.to_dict() if actor else None,
            "linked_open_pull_requests": [
                {
                    "provider_ref": pull_request["provider_ref"],
                    "repository": pull_request["repository"],
                    "url": pull_request["url"],
                    "is_draft": pull_request.get("is_draft", False),
                    "linked_by": pull_request.get("linked_by", []),
                }
                for pull_request in linked
            ],
        },
    ):
        return 0
    success(f"Moved {label} to In review")
    for pull_request in linked:
        routes = ", ".join(pull_request.get("linked_by", [])) or "link"
        field(pull_request["provider_ref"], f"{pull_request['url']} ({routes})")
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    if not provider.capabilities.request_closing:
        raise RuntimeError(
            f"canonical home {provider.provider_name!r} does not support closing a request"
        )
    with status(f"Checking request {args.issue}"):
        card = _read_card(provider, store, config, args.issue, pull_requests=not args.force)
    item = _issue(card, args.issue)
    number = item["number"]
    label = request_label(item)
    if item.get("state") == "CLOSED":
        raise RuntimeError(
            f"request {label} is already closed; "
            "run 'kanbanlan reconcile --apply' to settle its projection"
        )
    linked = item.get("linked_open_pull_requests") or []
    if linked and not args.force:
        references = ", ".join(pull_request["provider_ref"] for pull_request in linked)
        raise RuntimeError(
            f"request {label} still has an open pull request ({references}); "
            "merge or close it first, or pass --force to close the request anyway"
        )
    reason = "not_planned" if args.not_planned else "completed"
    claim = item.get("active_claim") or {}
    session = claim.get("session") or claim.get("author")
    timestamp = _utc_timestamp()
    with status(f"Closing request {label}"):
        if claim:
            # A closed request keeps no owner, so the claim ends before the
            # request does.
            provider.comment_request(
                number,
                (
                    f"RELEASED: {timestamp} — request closed: {args.reason}\n"
                    f"Session: {session or 'unknown'}"
                ),
            )
        provider.close_request(
            number,
            reason=reason,
            comment=f"CLOSED: {timestamp} — {args.reason}",
        )
        _set_state(provider, store, item, None, "Done")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="close",
            from_status=item.get("status"),
            to_status="Done",
            actor=actor,
            owner_session=session,
        )
        _refresh_after_mutation(root, store, provider)
    if _emit_result(
        args,
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "status": "Done",
            "close_reason": reason,
            "released_session": session,
            "actor_session": actor.to_dict() if actor else None,
        },
    ):
        return 0
    success(f"Closed {label} as {reason.replace('_', ' ')}")
    if session:
        field("released", session)
    return 0


def _cmd_handoff(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking active claim for request {args.issue}"):
        card = _read_card(provider, store, config, args.issue)
    item = _issue(card, args.issue)
    number = item["number"]
    label = request_label(item)
    if not item.get("active_claim"):
        raise RuntimeError(f"request {label} has no active claim")
    with status(f"Handing off {label} to {args.session}"):
        provider.comment_request(
            number,
            (
                f"HANDOFF: {_utc_timestamp()} — {args.reason}\n"
                f"Kanbanlan: {item.get('kanbanlan_id') or 'unassigned'}\n"
                f"Session: {args.session}\n"
                f"Branch: {args.branch}\n"
                f"Worktree: {Path(args.worktree).resolve()}"
            ),
        )
        _set_state(provider, store, item, "status:in-progress", "In progress")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="handoff",
            from_status="In progress",
            to_status="In progress",
            actor=actor,
            owner_session=args.session,
        )
        _refresh_after_mutation(root, store, provider)
    if _emit_result(
        args,
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "session": args.session,
            "actor_session": actor.to_dict() if actor else None,
        },
    ):
        return 0
    success(f"Handed off {label} to {args.session}")
    return 0
