"""Command-line entry point: parse, dispatch, and report errors."""

from __future__ import annotations

import argparse
import sys

from kanbanlan import __version__
from kanbanlan.cli.context import _context
from kanbanlan.cli.dispatch import _handler
from kanbanlan.cli.output import _emit_error, _friendly_error, _missing_command_hint
from kanbanlan.cli.parser import build_parser
from kanbanlan.legacy import apply_legacy_outbox, describe_change
from kanbanlan.runner import CommandError, RateLimitError, is_rate_limit_failure
from kanbanlan.ui import configure_color, configure_progress
from kanbanlan.updates import notify_if_update_available


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "admin":
        # 'kanbanlan admin claim' and the unlisted 'kanbanlan claim' are one command.
        args.command = args.admin_command
    configure_color(args.color)
    configure_progress(not args.json_output)
    if args.command not in {"upgrade", "session-hook"} and not args.json_output:
        notify_if_update_available(__version__)
    try:
        if args.command in LIFECYCLE_COMMANDS:
            _apply_legacy_outbox(args)
        return int(_handler(args.command)(args) or 0)
    except KeyboardInterrupt:
        _emit_error(args, "cancelled", kind="KeyboardInterrupt")
        return 130
    except FileNotFoundError as exc:
        command = exc.filename or "required command"
        _emit_error(
            args,
            f"{command!r} was not found",
            kind=exc.__class__.__name__,
            hint=_missing_command_hint(command),
        )
        return 1
    except (CommandError, RuntimeError, ValueError) as exc:
        message, hint = _friendly_error(exc)
        kind = exc.__class__.__name__
        # A quota refusal that surfaced as a failed gh command is reported as
        # one, so a caller can wait for the reset instead of treating it as final.
        if isinstance(exc, CommandError) and is_rate_limit_failure(exc.result):
            kind = RateLimitError.__name__
        reset_at = exc.reset_at if isinstance(exc, RateLimitError) else None
        _emit_error(args, message, kind=kind, hint=hint, reset_at=reset_at)
        return 1


# Commands that change a request; the first one run after an upgrade
# applies any changes an older version left queued in its outbox.
LIFECYCLE_COMMANDS = frozenset(
    {
        "start",
        "done",
        "drop",
        "capture",
        "triage",
        "claim",
        "release",
        "review",
        "close",
        "handoff",
        "rehome",
    }
)


def _apply_legacy_outbox(args: argparse.Namespace) -> None:
    """Apply and report changes an older version queued, once, before this command."""

    try:
        root, _, _, store = _context(args)
    except (CommandError, RuntimeError, OSError, ValueError):
        # Not a configured repository; the command itself reports that.
        return
    for entry in apply_legacy_outbox(root, store.directory):
        print(f"Kanbanlan: {describe_change(entry)}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
