"""The record command: durable repository records for one request."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kanbanlan.cli.cards import _issue, _local_view, _read_card
from kanbanlan.cli.context import _context
from kanbanlan.cli.output import _emit_result
from kanbanlan.config import Config
from kanbanlan.providers import CoordinationProvider
from kanbanlan.records import (
    create_record,
    current_branch,
    select_pull_request,
    write_record_from_pull_request,
)
from kanbanlan.snapshot import CacheStore
from kanbanlan.ui import status


def _cmd_record(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    if args.from_pr:
        return _record_from_pull_request(args, root, provider, store, config)
    snapshot = _local_view(root, store, provider, label=f"Loading request {args.request}")
    try:
        item = _issue(snapshot, args.request)
    except RuntimeError:
        # Not in the local view (newer than it, or left out of the read):
        # GitHub decides.
        item = {}
    if item.get("number") is None:
        # A record names the canonical issue.
        with status(f"Loading request {args.request}"):
            item = _issue(_read_card(provider, store, config, args.request), args.request)
    result = create_record(root, item)
    payload = {
        "action": result.action,
        "kanbanlan_id": item.get("kanbanlan_id"),
        "path": str(result.path),
    }
    if _emit_result(args, payload):
        return 0
    print(f"{result.action}: {result.path.relative_to(root)}")
    return 0


def _record_from_pull_request(
    args: argparse.Namespace,
    root: Path,
    provider: CoordinationProvider,
    store: CacheStore,
    config: Config,
) -> int:
    with status(f"Loading pull requests for {args.request}"):
        item = _issue(
            _read_card(provider, store, config, args.request, pull_requests=True),
            args.request,
        )
    pull_request = select_pull_request(item, current_branch(root))
    result, missing = write_record_from_pull_request(root, item, pull_request)
    payload = {
        "action": result.action,
        "kanbanlan_id": item.get("kanbanlan_id"),
        "path": str(result.path),
        "pull_request": pull_request.get("url"),
        "missing_sections": list(missing),
    }
    if not _emit_result(args, payload):
        print(f"{result.action}: {result.path.relative_to(root)} from {pull_request.get('url')}")
        if missing:
            print(f"missing from the pull request: {', '.join(missing)}", file=sys.stderr)
    return 1 if missing else 0
