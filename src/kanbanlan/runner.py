from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 60.0
TIMEOUT_RETURN_CODE = 124

# Upstream GitHub outages surface as gateway errors that succeed on a later
# attempt. Retrying is only safe for reads, so callers opt in per command.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
RETRY_BACKOFF_MULTIPLIER = 2.0
TRANSIENT_MARKERS = (
    "no server is currently available",
    "(http 502)",
    "(http 503)",
    "(http 504)",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
)


def is_transient_failure(result: CommandResult) -> bool:
    """Report whether a failed command looks like an upstream outage."""
    if result.returncode == 0:
        return False
    output = f"{result.stderr}\n{result.stdout}".lower()
    return any(marker in output for marker in TRANSIENT_MARKERS)


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        command = shlex.join(result.args)
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        super().__init__(f"{command}: {detail}")
        self.result = result


class Runner:
    def __init__(self, cwd: Path | None = None, env: Mapping[str, str | None] | None = None):
        self.cwd = cwd
        self.env = dict(env) if env is not None else None

    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture: bool = True,
        input_text: str | None = None,
        timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
        retry: bool = False,
    ) -> CommandResult:
        attempts = RETRY_ATTEMPTS if retry else 1
        delay = RETRY_BACKOFF_SECONDS
        for attempt in range(1, attempts + 1):
            result = self._execute(
                args,
                capture=capture,
                input_text=input_text,
                timeout=timeout,
            )
            if attempt == attempts or not is_transient_failure(result):
                break
            time.sleep(delay)
            delay *= RETRY_BACKOFF_MULTIPLIER
        if check and result.returncode:
            raise CommandError(result)
        return result

    def _execute(
        self,
        args: list[str],
        *,
        capture: bool,
        input_text: str | None,
        timeout: float | None,
    ) -> CommandResult:
        environment = None
        if self.env is not None:
            environment = os.environ.copy()
            for name, value in self.env.items():
                if value is None:
                    environment.pop(name, None)
                else:
                    environment[name] = value
        try:
            completed = subprocess.run(
                args,
                cwd=self.cwd,
                check=False,
                text=True,
                input=input_text,
                capture_output=capture,
                env=environment,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_output(exc.stdout)
            stderr = _timeout_output(exc.stderr).rstrip()
            duration = f"{exc.timeout:g}"
            detail = f"timed out after {duration} seconds"
            if stderr:
                detail = f"{stderr}\n{detail}"
            raise CommandError(
                CommandResult(
                    args=tuple(args),
                    returncode=TIMEOUT_RETURN_CODE,
                    stdout=stdout,
                    stderr=detail,
                )
            ) from exc
        return CommandResult(
            args=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def json(
        self,
        args: list[str],
        *,
        input_value: Any | None = None,
        retry: bool = False,
    ) -> Any:
        result = self.run(
            args,
            input_text=json.dumps(input_value) if input_value is not None else None,
            retry=retry,
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            detail = result.stdout.strip().replace("\n", " ")[:160]
            suffix = f": {detail}" if detail else " (empty output)"
            raise RuntimeError(f"{shlex.join(args)} returned invalid JSON{suffix}") from exc


def _timeout_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
