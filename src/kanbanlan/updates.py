from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

CHECK_INTERVAL_SECONDS = 3 * 24 * 60 * 60
PYPI_URL = "https://pypi.org/pypi/kanbanlan/json"
VERSION_RE = re.compile(r"^v?(?P<release>\d+(?:\.\d+)*)$")


def notify_if_update_available(
    current_version: str,
    *,
    state_path: Path | None = None,
    now: float | None = None,
) -> None:
    """Print an update notice at most once per check interval.

    Version checks are deliberately best-effort: network and cache failures must
    never prevent the requested Kanbanlan command from running.
    """
    if os.environ.get("KANBANLAN_NO_UPDATE_CHECK"):
        return

    checked_at = time.time() if now is None else now
    try:
        path = state_path or _state_path()
        check_is_due = _check_is_due(path, checked_at)
    except Exception:
        return
    if not check_is_due:
        return

    latest_version: str | None = None
    try:
        latest_version = _fetch_latest_version()
    except Exception:
        pass
    finally:
        _write_state(path, checked_at, latest_version)

    try:
        update_available = bool(
            latest_version and _is_newer_release(latest_version, current_version)
        )
    except Exception:
        return
    if update_available:
        print(
            f"Kanbanlan {latest_version} is available (installed: {current_version}). "
            "Run 'kanbanlan upgrade' to install it.",
            file=sys.stderr,
        )


def _state_path() -> Path:
    cache_root = os.environ.get("XDG_CACHE_HOME")
    if cache_root:
        return Path(cache_root) / "kanbanlan" / "version-check.json"
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "kanbanlan" / "version-check.json"
    return Path.home() / ".cache" / "kanbanlan" / "version-check.json"


def _check_is_due(path: Path, now: float) -> bool:
    try:
        state = json.loads(path.read_text())
        checked_at = float(state["checked_at"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return True
    return now - checked_at >= CHECK_INTERVAL_SECONDS


def _fetch_latest_version() -> str:
    request = Request(PYPI_URL, headers={"User-Agent": "kanbanlan-version-check"})
    with urlopen(request, timeout=2) as response:
        payload = json.load(response)
    version = payload.get("info", {}).get("version")
    if not isinstance(version, str):
        raise ValueError("PyPI response did not include a package version")
    return version


def _write_state(path: Path, checked_at: float, latest_version: str | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"checked_at": checked_at, "latest_version": latest_version},
                sort_keys=True,
            )
            + "\n"
        )
    except OSError:
        pass


def _is_newer_release(candidate: str, current: str) -> bool:
    candidate_match = VERSION_RE.fullmatch(candidate)
    current_match = re.match(r"^v?(?P<release>\d+(?:\.\d+)*)", current)
    if not candidate_match or not current_match:
        return False

    candidate_parts = tuple(int(part) for part in candidate_match["release"].split("."))
    current_parts = tuple(int(part) for part in current_match["release"].split("."))
    width = max(len(candidate_parts), len(current_parts))
    return candidate_parts + (0,) * (width - len(candidate_parts)) > current_parts + (0,) * (
        width - len(current_parts)
    )
