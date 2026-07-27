from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from time import sleep
from typing import TextIO

_COLOR_MODE = "auto"
_PROGRESS_ENABLED = True
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

RESET = "0"
BOLD = "1"
DIM = "2"
RED = "31"
GREEN = "32"
YELLOW = "33"
BLUE = "34"
MAGENTA = "35"
CYAN = "36"

STATUS_COLORS = {
    "Inbox": DIM,
    "Ready": GREEN,
    "In progress": YELLOW,
    "Blocked": RED,
    "In review": BLUE,
    "Done": MAGENTA,
    "fresh": GREEN,
    "stale": YELLOW,
    "missing": RED,
    "unknown": YELLOW,
    "error": RED,
}
PRIORITY_COLORS = {
    "priority:p0": RED,
    "priority:p1": YELLOW,
    "priority:p2": BLUE,
    "priority:p3": DIM,
}


def configure_color(mode: str) -> None:
    global _COLOR_MODE
    if mode not in {"auto", "always", "never"}:
        raise ValueError("color mode must be auto, always, or never")
    _COLOR_MODE = mode


def configure_progress(enabled: bool) -> None:
    global _PROGRESS_ENABLED
    _PROGRESS_ENABLED = enabled


def color_enabled(stream: TextIO | None = None) -> bool:
    output = stream or sys.stdout
    if _COLOR_MODE == "never" or os.environ.get("NO_COLOR") is not None:
        return False
    if _COLOR_MODE == "always":
        return True
    return bool(getattr(output, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


def style(value: object, *codes: str, stream: TextIO | None = None) -> str:
    text = str(value)
    if not codes or not color_enabled(stream):
        return text
    return f"\033[{';'.join(codes)}m{text}\033[{RESET}m"


def heading(value: str) -> None:
    print(style(value, BOLD, CYAN))


def section(value: str) -> None:
    print(f"\n{style(value, BOLD, CYAN)}")


def field(label: str, value: object) -> None:
    print(f"  {style(label + ':', DIM)} {value}")


def success(value: str) -> None:
    print(f"{style('✓', GREEN)} {value}")


def warning(value: str) -> None:
    print(f"{style('!', YELLOW, BOLD)} {value}")


def error(value: str, *, hint: str | None = None) -> None:
    stream = sys.stderr
    print(f"{style('Error:', RED, BOLD, stream=stream)} {value}", file=stream)
    if hint:
        print(f"{style('Hint:', CYAN, BOLD, stream=stream)} {hint}", file=stream)


def status_value(value: str) -> str:
    code = STATUS_COLORS.get(value)
    return style(value, code) if code else value


def priority_value(value: str) -> str:
    code = PRIORITY_COLORS.get(value)
    return style(value, code) if code else value


@dataclass
class Status:
    """Show durable progress in logs and an animated spinner in a terminal."""

    label: str
    enabled: bool = True
    stream: TextIO | None = None

    def __post_init__(self) -> None:
        self._stream = self.stream or sys.stderr
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tty = bool(getattr(self._stream, "isatty", lambda: False)())

    def __enter__(self) -> Status:
        if not self.enabled:
            return self
        if self._tty:
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()
        else:
            print(f"{style('→', CYAN, stream=self._stream)} {self.label}", file=self._stream)
        return self

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join()
        marker = style("✓", GREEN, stream=self._stream)
        if exc_type is not None:
            marker = style("✗", RED, stream=self._stream)
        prefix = "\r\033[2K" if self._tty else ""
        print(f"{prefix}{marker} {self.label}", file=self._stream, flush=True)

    def _animate(self) -> None:
        index = 0
        while not self._stop.is_set():
            frame = style(_SPINNER_FRAMES[index % len(_SPINNER_FRAMES)], CYAN, stream=self._stream)
            print(f"\r\033[2K{frame} {self.label}", end="", file=self._stream, flush=True)
            index += 1
            sleep(0.08)


def status(label: str, *, enabled: bool = True) -> Status:
    return Status(label, enabled=enabled and _PROGRESS_ENABLED)
