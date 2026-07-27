from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    def __init__(self, cwd: Path | None = None, env: Mapping[str, str] | None = None):
        self.cwd = cwd
        self.env = dict(env) if env is not None else None

    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            args,
            cwd=self.cwd,
            check=False,
            text=True,
            input=input_text,
            capture_output=capture,
            env={**os.environ, **self.env} if self.env is not None else None,
        )
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
