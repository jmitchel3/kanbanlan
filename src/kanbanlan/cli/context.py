"""Repository, provider, and agent-session context for commands."""

from __future__ import annotations

import argparse
from pathlib import Path

from kanbanlan.cli.output import _utc_timestamp
from kanbanlan.config import Config, cache_dir, find_repo_root
from kanbanlan.providers import CoordinationProvider, create_provider
from kanbanlan.sessions import (
    AgentSession,
    SessionContextStore,
    activity_comment,
    detect_agent_session,
)
from kanbanlan.snapshot import CacheStore


def _root(args: argparse.Namespace) -> Path:
    start = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    return find_repo_root(start)


def _context(
    args: argparse.Namespace,
) -> tuple[Path, Config, CoordinationProvider, CacheStore]:
    root = _root(args)
    config = Config.load(root)
    provider = create_provider(root, config)
    store = CacheStore(config, cache_dir(root))
    return root, config, provider, store


def _actor_session(
    args: argparse.Namespace,
    root: Path,
    config: Config,
) -> AgentSession | None:
    explicit = getattr(args, "actor_session", None)
    if not config.session_tracking_enabled():
        if explicit:
            raise RuntimeError(
                "--actor-session requires session tracking; set "
                "[session_tracking].enabled = true or KANBANLAN_SESSION_TRACKING=true"
            )
        return None
    return detect_agent_session(
        explicit=explicit,
        store=SessionContextStore(cache_dir(root)),
        root=root,
    )


def _record_session_activity(
    *,
    config: Config,
    provider: CoordinationProvider,
    reference: int | str,
    action: str,
    from_status: str | None,
    to_status: str | None,
    actor: AgentSession | None,
    owner_session: str | None = None,
    repository: str | None = None,
) -> None:
    if not config.session_tracking_enabled():
        return
    provider.comment_request(
        reference,
        activity_comment(
            action=action,
            at=_utc_timestamp(),
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            owner_session=owner_session,
        ),
        repository=repository,
    )
