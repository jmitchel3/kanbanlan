"""Resolve a command name to the function that runs it."""

from __future__ import annotations

from typing import Any


def _handler(command: str) -> Any:
    # Imported here: the verbs run other commands through this lookup.
    from kanbanlan.cli import cleanup, lifecycle, queue, records, sessions, setup, verbs

    name = f"_cmd_{command.replace('-', '_')}"
    for module in (setup, queue, cleanup, lifecycle, sessions, records, verbs):
        handler = getattr(module, name, None)
        if handler is not None:
            return handler
    raise KeyError(name)
