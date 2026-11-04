"""Shared result, error, and formatting helpers for commands."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kanbanlan.runner import CommandError, RateLimitError, is_transient_failure
from kanbanlan.ui import error


def _write_json(value: dict[str, Any], *, stream: Any = None) -> None:
    # Resolve the stream on each call so a redirected stdout is honored.
    target = stream if stream is not None else sys.stdout
    json.dump(value, target, indent=2, sort_keys=True)
    target.write("\n")


def _emit_result(args: argparse.Namespace, value: dict[str, Any]) -> bool:
    # A command run as one step of another hands its result to the caller.
    sink = getattr(args, "result_sink", None)
    if sink is not None:
        sink.append(value)
        return True
    if not args.json_output:
        return False
    _write_json({"ok": True, "result": value})
    return True


def _emit_error(
    args: argparse.Namespace,
    message: str,
    *,
    kind: str,
    hint: str | None = None,
    reset_at: str | None = None,
) -> None:
    if args.json_output:
        _write_json(
            {
                "ok": False,
                "error": {
                    "kind": kind,
                    "message": message,
                    "hint": hint,
                    **({"reset_at": reset_at} if reset_at else {}),
                },
            },
            stream=sys.stderr,
        )
        return
    error(message, hint=hint)


def _missing_command_hint(command: str) -> str:
    name = Path(command).name
    if name == "gh":
        return "Install GitHub CLI from https://cli.github.com, then run 'kanbanlan auth'."
    if name == "git":
        return "Install Git and rerun the command from a Git repository."
    if name == "uv":
        return "Install uv from https://docs.astral.sh/uv/."
    return f"Install {name!r} and make sure it is available on PATH."


def _friendly_error(exc: Exception) -> tuple[str, str | None]:
    if isinstance(exc, RateLimitError):
        detail = f" (resets {exc.reset_at})" if exc.reset_at else ""
        return str(exc), (
            "GitHub is rate limiting this account"
            + detail
            + ". Cached snapshots keep serving; wait for the quota to reset before refreshing."
        )
    if not isinstance(exc, CommandError):
        message = str(exc)
        hint = None
        if ".kanbanlan.toml" in message and "missing" in message:
            hint = "Run 'kanbanlan init' from the repository root."
        elif "not a git repository" in message.lower():
            hint = "Run inside a Git repository or pass '-C /path/to/repository'."
        return message, hint

    result = exc.result
    command = shlex.join(result.args)
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    normalized = detail.lower()
    hint = f"Run `{command}` directly for more detail."
    if result.args and result.args[0] == "gh":
        if is_transient_failure(result):
            hint = (
                "GitHub returned a temporary server error and retries did not clear it. "
                "Check https://www.githubstatus.com, then run this command again."
            )
        elif "unknown owner type" in normalized:
            hint = (
                "The GitHub CLI reports this whenever it cannot resolve the owner, "
                "including during a GitHub outage. Confirm the owner name, check "
                "https://www.githubstatus.com, then run this command again."
            )
        elif any(value in normalized for value in ("auth", "credential", "401", "scope")):
            hint = "Run 'kanbanlan auth' to repair GitHub login and Project access."
        elif any(
            value in normalized for value in ("connect", "network", "resolve host", "timed out")
        ):
            hint = "Check network access to GitHub, then retry this command."
    elif result.args[:3] == ("git", "rev-parse", "--show-toplevel"):
        return (
            "the selected directory is not inside a Git repository",
            "Run inside a Git repository or pass '-C /path/to/repository'.",
        )
    return f"command failed ({result.returncode}): {command}\n{detail}", hint


def _rate_limit_text(rate: dict[str, Any]) -> str:
    """Describe the quota the last snapshot read reported."""

    reset = f", resets {rate['resetAt']}" if rate.get("resetAt") else ""
    if rate.get("resource") == "core":
        # The queue read spends REST requests, and none while nothing changed.
        unchanged = rate.get("not_modified")
        detail = f", {unchanged} of {rate.get('requests')} reads unchanged" if unchanged else ""
        return f"{rate['remaining']} REST requests remaining{reset}{detail}"
    return f"{rate['remaining']} GraphQL points remaining{reset}"


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
