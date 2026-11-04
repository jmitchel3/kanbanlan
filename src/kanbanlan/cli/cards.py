"""Read and write single request cards without a board-wide refresh."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from kanbanlan.config import Config
from kanbanlan.domain import request_label, resolve_request_item
from kanbanlan.identity import normalize_kanbanlan_id
from kanbanlan.providers import CoordinationProvider
from kanbanlan.runner import CommandError
from kanbanlan.snapshot import CacheStore
from kanbanlan.ui import status

# A request the local view does not have yet is read from GitHub, waiting at
# most this long in all.
CARD_READ_TIMEOUT_SECONDS = 15.0


def _local_view(
    root: Path,
    store: CacheStore,
    provider: CoordinationProvider,
    *,
    label: str,
) -> dict[str, Any]:
    """Return the shared snapshot, refreshing it first when it is stale."""

    with status(label):
        return store.ensure(provider)


def _refresh_after_mutation(
    root: Path,
    store: CacheStore,
    provider: CoordinationProvider,
) -> None:
    """Mark the shared snapshot stale after a command changed the board.

    The command already wrote its card to GitHub and reads nothing more, so
    it does not wait for a board read: the next read that needs the
    snapshot refreshes it instead of serving the pre-mutation state.
    """

    store.invalidate()


def _set_state(
    provider: CoordinationProvider,
    store: CacheStore,
    item: dict[str, Any],
    label: str | None,
    status: str,
) -> None:
    """Move one card: its status label and its Project Status, nothing else."""

    item_id = item.get("project_item_id")
    if not item_id:
        raise _NotOnBoard(f"request {request_label(item)} is not on the configured kanban home")
    # The label and the Project Status are separate records, so both writes
    # run at once; each still raises exactly as it would alone.
    with ThreadPoolExecutor(max_workers=2) as executor:
        label_write = executor.submit(provider.set_request_status, item["number"], label)
        status_write = executor.submit(_set_card_status, provider, store, item_id, status)
        label_write.result()
        status_write.result()


def _set_card_status(
    provider: CoordinationProvider,
    store: CacheStore,
    item_id: str,
    status: str,
) -> None:
    """Write one Project item's Status without reading the board.

    The Status field comes from the cached snapshot, or from a metadata-only
    read when the cache is missing or its field ids have gone stale.
    """

    cached = (store.snapshot() or {}).get("project")
    try:
        if not cached or not cached.get("fields"):
            raise RuntimeError("no cached Project fields")
        provider.set_projection_status(item_id, cached, status)
    except (CommandError, RuntimeError):
        provider.set_projection_status(item_id, provider.projection_metadata(), status)


def _card_address(
    provider: CoordinationProvider,
    store: CacheStore,
    config: Config,
    reference: int | str,
    *,
    timeout: float | None = None,
) -> tuple[str, int]:
    """Find the repository and number ``reference`` names, without the board.

    A provider reference carries its number. A Kanbanlan ID is looked up in
    the cached snapshot, and only a request newer than that costs a search.
    """

    value = str(reference).strip()
    kanbanlan_id = normalize_kanbanlan_id(value)
    if kanbanlan_id:
        snapshot = store.snapshot()
        cached = None
        if snapshot:
            try:
                cached = _issue(snapshot, kanbanlan_id)
            except RuntimeError:
                cached = None
        if cached and cached.get("number") is not None:
            return cached.get("repository") or config.repository, cached["number"]
        found = provider.find_request(kanbanlan_id, repository=config.repository, timeout=timeout)
        if not found:
            raise _NotOnBoard(f"request {value!r} is not on the configured kanban home")
        return found.get("repository") or config.repository, found["number"]
    match = CARD_REFERENCE_RE.fullmatch(value)
    if not match:
        raise _NotOnBoard(f"request {value!r} is not on the configured kanban home")
    return match.group("repository") or config.repository, int(match.group("number"))


def _read_card(
    provider: CoordinationProvider,
    store: CacheStore,
    config: Config,
    reference: int | str,
    *,
    pull_requests: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Read one request live as a one-card snapshot.

    This is how a lifecycle command checks GitHub: one card's reads, never
    the board and never the refresh lock, so it costs the same on a board of
    ten cards or ten thousand. ``timeout`` bounds the whole read, lookup of
    a Kanbanlan ID included.

    Only a request on the configured Project counts, as it would in a board
    read: an issue that is not on it, or whose item is archived, is refused
    rather than acted on and silently added.
    """

    deadline = None if timeout is None else time.monotonic() + timeout
    repository, number = _card_address(provider, store, config, reference, timeout=timeout)
    card = provider.read_request(
        number,
        repository=repository,
        pull_requests=pull_requests,
        timeout=None if deadline is None else max(deadline - time.monotonic(), 1.0),
    )
    try:
        # The number may have come from a Kanbanlan ID; the card must carry it.
        item = _issue(card, reference)
    except RuntimeError as exc:
        raise _NotOnBoard(str(exc)) from None
    if not item.get("project_item_id"):
        raise _NotOnBoard(f"request {reference!r} is not on the configured kanban home")
    return card


def _verify_claim(
    provider: CoordinationProvider,
    store: CacheStore,
    config: Config,
    number: int,
    session: str,
) -> dict[str, Any]:
    """Return the card as its claim comments settle it, after posting a claim.

    The earliest unreleased CLAIM comment wins however many sessions raced,
    so reading this one card decides it. A read that does not show any claim
    yet is repeated briefly rather than mistaken for a lost race.
    """

    item: dict[str, Any] = {}
    for delay in CLAIM_VERIFY_DELAYS:
        time.sleep(delay)
        item = _issue(_read_card(provider, store, config, number), number)
        if item.get("active_claim"):
            break
    claim = item.get("active_claim") or {}
    if claim.get("session") != session:
        return item
    # A replica that had not yet seen an earlier claim could show this one
    # winning; one more read a moment later narrows that window.
    time.sleep(CLAIM_CONFIRM_DELAY)
    return _issue(_read_card(provider, store, config, number), number)


# A provider reference to one issue: 12, #12, OWNER/REPO#12, or its qualified
# github:OWNER/REPO#12 form.
CARD_REFERENCE_RE = re.compile(
    r"(?:github:)?(?:(?P<repository>[^/#\s]+/[^/#\s]+)#|#)?(?P<number>\d+)"
)


# Pauses before each read of a just-posted claim, until one shows it, and
# before the read that confirms this session won.
CLAIM_VERIFY_DELAYS = (0.0, 0.5, 1.0)


CLAIM_CONFIRM_DELAY = 1.0


class _NotOnBoard(RuntimeError):
    """GitHub answered: the reference names no card on the configured Project."""


def _issue(snapshot: dict[str, Any], reference: int | str) -> dict[str, Any]:
    return resolve_request_item(snapshot, reference)
