from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 60.0
TIMEOUT_RETURN_CODE = 124


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
        result = CommandResult(
            args=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if check and result.returncode:
            raise CommandError(result)
        return result

    def json(self, args: list[str], *, input_value: Any | None = None) -> Any:
        result = self.run(
            args,
            input_text=json.dumps(input_value) if input_value is not None else None,
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
