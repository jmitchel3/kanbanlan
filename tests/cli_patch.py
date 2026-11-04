"""Patch a name across every module of the split CLI package."""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import Any
from unittest import mock

import kanbanlan.cli


def cli_modules() -> list[ModuleType]:
    return [
        importlib.import_module(f"kanbanlan.cli.{info.name}")
        for info in pkgutil.iter_modules(kanbanlan.cli.__path__)
        if info.name != "__main__"
    ]


class patch_cli:  # noqa: N801 - used like mock.patch
    """Patch ``name`` in each CLI module that defines or imports it.

    The CLI modules share helpers by importing them, so one replacement must
    reach every module that looks the name up. Works as a context manager or
    with ``start`` and ``stop``, like ``mock.patch``.
    """

    def __init__(self, name: str, new: Any = mock.DEFAULT, **kwargs: Any) -> None:
        modules = [module for module in cli_modules() if name in vars(module)]
        if not modules:
            raise AttributeError(f"no kanbanlan.cli module has {name!r}")
        self._first = mock.patch.object(modules[0], name, new, **kwargs)
        self._modules = modules[1:]
        self._name = name
        self._others: list[Any] = []

    def start(self) -> Any:
        value = self._first.start()
        self._others = [mock.patch.object(module, self._name, value) for module in self._modules]
        for patcher in self._others:
            patcher.start()
        return value

    def stop(self) -> None:
        for patcher in reversed(self._others):
            patcher.stop()
        self._first.stop()

    def __enter__(self) -> Any:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
