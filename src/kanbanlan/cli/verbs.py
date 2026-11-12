"""The start, done, and drop verbs, built from lifecycle steps."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from kanbanlan.accounts import AccountRunner
from kanbanlan.cli.cards import (
    CARD_READ_TIMEOUT_SECONDS,
    CARD_REFERENCE_RE,
    _issue,
    _local_view,
    _read_card,
)
from kanbanlan.cli.cleanup import _apply_cleanup, _plan_worktree_cleanup
from kanbanlan.cli.context import _actor_session, _context
from kanbanlan.cli.dispatch import _handler
from kanbanlan.cli.output import _emit_result
from kanbanlan.cli.parser import build_parser
from kanbanlan.config import Config
from kanbanlan.domain import request_label
from kanbanlan.identity import normalize_kanbanlan_id
from kanbanlan.providers import CoordinationProvider
from kanbanlan.runner import Runner
from kanbanlan.snapshot import CacheStore
from kanbanlan.ui import field, section, status, success, warning
from kanbanlan.worktrees import checkout_identity, removals

# Commands that take --actor-session, so a verb's own one passes through.
ACTOR_STEP_COMMANDS = frozenset({"capture", "triage", "claim", "release", "review", "close"})


# Pauses before each review attempt after 'done' opens a pull request, while
# GitHub links its closing reference to the request.
DONE_LINK_DELAYS = (0.0, 2.0, 3.0, 5.0)


DEFAULT_DROP_REASON = "dropped by its owner"


def _step_args(
    args: argparse.Namespace,
    argv: list[str],
    *,
    capture: bool = True,
) -> argparse.Namespace:
    """Parse ``argv`` as a command that runs as one step of ``args``.

    A captured step hands its result back through ``result_sink`` instead of
    printing it; an uncaptured one prints exactly as that command would.
    """

    prefix = ["--color", args.color]
    if args.repo_root:
        prefix.extend(["-C", args.repo_root])
    if capture or args.json_output:
        prefix.append("--json")
    command = list(argv)
    if getattr(args, "actor_session", None) and command[0] in ACTOR_STEP_COMMANDS:
        command.append(f"--actor-session={args.actor_session}")
    step = build_parser().parse_args([*prefix, *command])
    if capture:
        step.result_sink = []
    return step


def _run_step(args: argparse.Namespace, argv: list[str]) -> dict[str, Any]:
    step = _step_args(args, argv)
    _handler(step.command)(step)
    if not step.result_sink:
        raise RuntimeError(f"kanbanlan {argv[0]} reported no result")
    return step.result_sink[-1]


def _looks_like_reference(value: str) -> bool:
    text = value.strip()
    return bool(normalize_kanbanlan_id(text) or CARD_REFERENCE_RE.fullmatch(text))


def _session_claims(
    requests: list[dict[str, Any]],
    session: str | None,
) -> list[dict[str, Any]]:
    if not session:
        return []
    return [
        request
        for request in requests
        if request.get("state", "OPEN") == "OPEN"
        and (request.get("active_claim") or {}).get("session") == session
    ]


def _issues(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in snapshot.get("items", []) if item.get("type") == "ISSUE"]


def _live_claims(
    store: CacheStore,
    provider: CoordinationProvider,
    session: str | None,
) -> dict[str, Any]:
    """Read this repository's claims live before a session claims another card.

    The local view may be stale or incomplete (a card the Project read left
    out), and a claim missing from it would let one session hold two cards.
    A read begun after this call reflects every claim already posted; it
    fails closed, refusing the start, when GitHub cannot be read.
    """

    if not session:
        return {}
    with status("Checking this session's claims on GitHub"):
        return store.refresh(provider)


def _same_request(left: dict[str, Any], right: dict[str, Any] | None) -> bool:
    if right is None:
        return False
    if left.get("kanbanlan_id") and left.get("kanbanlan_id") == right.get("kanbanlan_id"):
        return True
    return bool(left.get("provider_ref")) and left.get("provider_ref") == right.get("provider_ref")


def _start_target(
    provider: CoordinationProvider,
    store: CacheStore,
    config: Config,
    view: dict[str, Any],
    reference: str,
) -> dict[str, Any]:
    try:
        return _issue(view, reference)
    except RuntimeError:
        pass
    # Newer than the local view: GitHub decides, within a few seconds.
    with status(f"Checking request {reference}"):
        card = _read_card(provider, store, config, reference, timeout=CARD_READ_TIMEOUT_SECONDS)
    return _issue(card, reference)


def _prune_orphaned_worktrees(
    root: Path,
    config: Config,
    provider: CoordinationProvider,
    store: CacheStore,
    view: dict[str, Any],
) -> list[dict[str, Any]]:
    """Remove only what 'cleanup --apply' would remove, and report it.

    The local view finds candidates without touching GitHub. Any candidate
    is then decided exactly as cleanup decides, against a fresh board read and
    a current default branch.
    """

    runner = Runner(root)
    with status("Checking for orphaned request worktrees"):
        candidates = removals(
            _plan_worktree_cleanup(root, config, runner, view, force=False, fetch=False)
        )
    if not candidates:
        return []
    with status("Confirming orphaned worktrees against GitHub"):
        snapshot = store.refresh(provider)
        confirmed = removals(_plan_worktree_cleanup(root, config, runner, snapshot, force=False))
    if not confirmed:
        return []
    return _apply_cleanup(runner, confirmed)


def _cmd_start(args: argparse.Namespace) -> int:
    started = time.monotonic()
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    session = args.session or (actor.reference if actor else None)
    view = _local_view(root, store, provider, label="Loading the local board")
    reference = args.request.strip()
    target: dict[str, Any] | None = None
    if _looks_like_reference(reference):
        target = _start_target(provider, store, config, view, reference)
        label = request_label(target)
        if target.get("state") != "OPEN":
            raise RuntimeError(f"request {label} is not open")
        claim = target.get("active_claim") or {}
        if claim and session and claim.get("session") == session:
            return _emit_start(
                args,
                started,
                target,
                {
                    "kanbanlan_id": target.get("kanbanlan_id"),
                    "provider_ref": target.get("provider_ref"),
                    "session": session,
                    "branch": claim.get("branch"),
                    "worktree": claim.get("worktree"),
                },
                resumed=True,
            )
        if claim:
            owner = claim.get("session") or claim.get("author") or "another session"
            raise RuntimeError(f"request {label} is already claimed by {owner}")
        if target.get("status") not in {"Inbox", "Ready"}:
            raise RuntimeError(
                f"request {label} is {target.get('status')!r}; only Inbox or Ready "
                "requests can be started"
            )

    overlap = _run_step(args, ["overlap"])
    requests = overlap.get("open_requests", [])
    for owned in [
        *_session_claims(_issues(view), session),
        *_session_claims(_issues(_live_claims(store, provider, session)), session),
        *_session_claims(requests, session),
    ]:
        if not _same_request(owned, target):
            raise RuntimeError(
                f"session {session} already owns {request_label(owned)}; finish it with "
                "'kanbanlan done' or release it with 'kanbanlan drop' first"
            )
    others = [
        {
            "kanbanlan_id": request.get("kanbanlan_id"),
            "provider_ref": request.get("provider_ref"),
            "repository": request.get("repository"),
            "title": request.get("title"),
            "status": request.get("status"),
            "session": request["active_claim"].get("session"),
            "touchpoints": request["active_claim"].get("touchpoints"),
            "linked_open_pull_requests": [
                pull_request.get("url")
                for pull_request in request.get("linked_open_pull_requests", [])
            ],
        }
        for request in requests
        if request.get("active_claim") and not _same_request(request, target)
    ]

    pruned = _prune_orphaned_worktrees(root, config, provider, store, view)

    captured = None
    if target is None:
        captured = _run_step(
            args,
            [
                "capture",
                f"--priority={args.priority}",
                f"--body={args.body}",
                "--",
                reference,
            ],
        )
        reference = captured["kanbanlan_id"]
        status_name = "Inbox"
    else:
        reference = _sync_reference(target)
        status_name = target.get("status")
    triaged = status_name == "Inbox"
    if triaged:
        _run_step(args, ["triage", reference])
    claim_argv = ["claim", reference, f"--touchpoints={args.touchpoints}"]
    if args.session:
        claim_argv.append(f"--session={args.session}")
    claimed = _run_step(args, claim_argv)
    request = target or {
        "kanbanlan_id": captured["kanbanlan_id"] if captured else None,
        "provider_ref": (captured or {}).get("provider_ref"),
        "title": args.request.strip(),
    }
    return _emit_start(
        args,
        started,
        request,
        claimed,
        captured=captured is not None,
        triaged=triaged,
        others=others,
        unlinked=[
            {
                "provider_ref": pull_request.get("provider_ref"),
                "title": pull_request.get("title"),
                "url": pull_request.get("url"),
            }
            for pull_request in overlap.get("unlinked_open_pull_requests", [])
        ],
        pruned=pruned,
    )


def _emit_start(
    args: argparse.Namespace,
    started: float,
    request: dict[str, Any],
    claimed: dict[str, Any],
    *,
    resumed: bool = False,
    captured: bool = False,
    triaged: bool = False,
    others: list[dict[str, Any]] | None = None,
    unlinked: list[dict[str, Any]] | None = None,
    pruned: list[dict[str, Any]] | None = None,
) -> int:
    others = others or []
    result = {
        "request": {
            "kanbanlan_id": request.get("kanbanlan_id"),
            "provider_ref": claimed.get("provider_ref") or request.get("provider_ref"),
            "title": request.get("title"),
        },
        "captured": captured,
        "triaged": triaged,
        "resumed": resumed,
        "pruned": pruned or [],
        "overlap": {
            "active_claims": others,
            "unlinked_open_pull_requests": unlinked or [],
        },
        "claim": claimed,
        "session": claimed.get("session"),
        "branch": claimed.get("branch"),
        "worktree": claimed.get("worktree"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if _emit_result(args, result):
        return 0
    label = request.get("kanbanlan_id") or request.get("provider_ref") or ""
    if captured:
        success(f"Captured {label} {request.get('title')}")
    if triaged:
        success(f"Moved {label} to Ready")
    for value in pruned or []:
        success(f"Pruned orphaned worktree {value['path']} ({value['reason']})")
    if others:
        warning(
            f"{len(others)} other active claim(s) on the Project; "
            "check them for semantic overlap before you build"
        )
        for other in others:
            section(f"{other['provider_ref']} {other['title']}")
            field("Session", other.get("session") or "unknown")
            field("Touchpoints", other.get("touchpoints") or "not declared")
    if resumed:
        success(f"{label} is already claimed by {claimed.get('session')}; continuing it")
    else:
        success(f"Claimed {label} as {claimed.get('session')}")
    if claimed.get("branch"):
        print(f"branch: {claimed['branch']}")
    # Last, so an agent can cd into it.
    print(claimed.get("worktree") or "")
    return 0


def _checkout_request(
    root: Path,
    store: CacheStore,
    provider: CoordinationProvider,
) -> str:
    """Return the request the current worktree or branch was claimed for."""

    branch = Runner(root).run(["git", "branch", "--show-current"]).stdout.strip()
    identity = checkout_identity(root, branch)
    if identity:
        return identity
    view = _local_view(root, store, provider, label="Finding the request for this worktree")
    matches = [
        item
        for item in view.get("items", [])
        if item.get("type") == "ISSUE"
        and item.get("active_claim")
        and (
            (branch and item["active_claim"].get("branch") == branch)
            or item["active_claim"].get("worktree") == str(root)
        )
    ]
    if len(matches) == 1:
        return _sync_reference(matches[0])
    raise RuntimeError(
        "this checkout is not claimed for a request; pass its Kanbanlan ID or #number"
    )


def _cmd_drop(args: argparse.Namespace) -> int:
    if not args.close and (args.not_planned or args.force):
        raise RuntimeError("--not-planned and --force only apply with --close")
    if args.close and not args.reason:
        raise RuntimeError("--close requires --reason")
    reference = args.request
    if not reference:
        root, _, provider, store = _context(args)
        reference = _checkout_request(root, store, provider)
    if args.close:
        argv = ["close", reference, f"--reason={args.reason}"]
        if args.not_planned:
            argv.append("--not-planned")
        if args.force:
            argv.append("--force")
    else:
        argv = ["release", reference, f"--reason={args.reason or DEFAULT_DROP_REASON}"]
    step = _step_args(args, argv, capture=False)
    return int(_handler(step.command)(step) or 0)


def _gh_runner(root: Path, config: Config) -> Runner:
    return AccountRunner(root, config)


def _open_pull_request(gh: Runner, config: Config, branch: str) -> dict[str, Any] | None:
    output = gh.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            config.repository,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url,isDraft",
            "--limit",
            "1",
        ]
    ).stdout
    values = json.loads(output or "[]")
    return values[0] if values else None


def _pull_request_body(root: Path, config: Config, item: dict[str, Any]) -> str:
    """Fill the repository's pull request template with the request's identity."""

    kanbanlan_line = f"Kanbanlan: {item.get('kanbanlan_id') or 'unassigned'}"
    repository = item.get("repository") or config.repository
    number = item["number"]
    target = f"#{number}" if repository == config.repository else f"{repository}#{number}"
    closes_line = f"Closes {target}"
    template = root / ".github" / "pull_request_template.md"
    try:
        body = template.read_text(encoding="utf-8")
    except OSError:
        body = f"## Summary\n\n{item.get('title') or ''}\n"
    body, kanbanlan_found = re.subn(r"(?m)^Kanbanlan:.*$", kanbanlan_line, body, count=1)
    body, closes_found = re.subn(r"(?m)^Closes\b.*$", closes_line, body, count=1)
    missing = [
        line
        for line, found in ((kanbanlan_line, kanbanlan_found), (closes_line, closes_found))
        if not found
    ]
    if missing:
        body = body.rstrip("\n") + "\n\n" + "\n".join(missing) + "\n"
    return body


def _review_step(args: argparse.Namespace, reference: str, *, wait: bool) -> dict[str, Any]:
    delays = DONE_LINK_DELAYS if wait else (0.0,)
    for attempt, delay in enumerate(delays, start=1):
        time.sleep(delay)
        try:
            return _run_step(args, ["review", reference])
        except RuntimeError as exc:
            if attempt == len(delays) or "no linked open pull request" not in str(exc):
                raise
    raise AssertionError("unreachable")


def _cmd_done(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    reference = args.request or _checkout_request(root, store, provider)
    view = _local_view(root, store, provider, label=f"Loading request {reference}")
    try:
        item = _issue(view, reference)
    except RuntimeError:
        item = {}
    if item.get("number") is None:
        # Newer than the local view: GitHub decides.
        with status(f"Loading request {reference}"):
            item = _issue(_read_card(provider, store, config, reference), reference)
    label = request_label(item)
    if item.get("state") != "OPEN":
        raise RuntimeError(f"request {label} is not open")
    runner = Runner(root)
    branch = (item.get("active_claim") or {}).get("branch") or runner.run(
        ["git", "branch", "--show-current"]
    ).stdout.strip()
    if not branch or branch == config.default_branch:
        raise RuntimeError(
            f"request {label} has no work branch; run 'kanbanlan done' from its worktree"
        )
    gh = _gh_runner(root, config)
    with status(f"Looking for the open pull request from {branch}"):
        pull_request = _open_pull_request(gh, config, branch)
    created = pull_request is None
    if pull_request is None:
        with status(f"Pushing {branch}"):
            runner.run(["git", "push", "--set-upstream", "origin", branch])
        command = [
            "gh",
            "pr",
            "create",
            "--repo",
            config.repository,
            "--base",
            config.default_branch,
            "--head",
            branch,
            "--title",
            args.title or item.get("title") or label,
            "--body",
            _pull_request_body(root, config, item),
        ]
        if args.draft:
            command.append("--draft")
        with status(f"Opening a pull request for {label}"):
            output = gh.run(command).stdout.strip()
        pull_request = {"url": output.splitlines()[-1] if output else None}
    review = _review_step(args, _sync_reference(item), wait=created)
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "provider_ref": item.get("provider_ref"),
        "branch": branch,
        "pull_request": pull_request,
        "created_pull_request": created,
        "review": review,
    }
    if _emit_result(args, result):
        return 0
    verb = "Opened" if created else "Found"
    success(f"{verb} pull request {pull_request.get('url')}")
    success(f"Moved {label} to In review")
    return 0


def _sync_reference(item: dict[str, Any]) -> str:
    return item.get("kanbanlan_id") or item["provider_ref"]
