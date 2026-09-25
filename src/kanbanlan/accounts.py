"""Choose, deterministically, which gh account Kanbanlan acts as.

gh can hold several logged-in accounts per host, and its active account is
whichever one was used last, often one that belongs to a different owner.
Acting as it would make every write's identity an accident. Instead each
repository resolves to one account, in this order:

1. ``KANBANLAN_GITHUB_ACCOUNT``, for one command or one shell.
2. A user-level binding for the repository, then for its owner, written by
   ``kanbanlan account use``.
3. An automatic choice, only when it is unambiguous: the repository owner
   or the Project owner is itself a logged-in account, or exactly one
   account is logged in. The choice is persisted as a binding, so the
   ``gh auth status`` call it needs is paid once.

Anything else is an error naming the logged-in accounts. An explicit
``GH_TOKEN`` or ``GITHUB_TOKEN`` in the environment (CI, a scoped token) is
honored as is when no account was requested, since it already names one
identity.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kanbanlan.config import Config
from kanbanlan.locks import FileLock
from kanbanlan.registry import state_dir
from kanbanlan.runner import CommandResult, Runner

ACCOUNT_ENV = "KANBANLAN_GITHUB_ACCOUNT"
TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")
ACCOUNTS_SCHEMA_VERSION = 1

SOURCE_ENVIRONMENT = "environment"
SOURCE_REPOSITORY = "repository binding"
SOURCE_OWNER = "owner binding"
SOURCE_OWNER_MATCH = "automatic: owner is a logged-in account"
SOURCE_ONLY_ACCOUNT = "automatic: only logged-in account"


@dataclass(frozen=True)
class Account:
    hostname: str
    login: str
    source: str


def repository_key(repository: str) -> str:
    return f"repo:{repository.lower()}"


def owner_key(owner: str) -> str:
    return f"owner:{owner.lower()}"


class AccountStore:
    """User-scoped bindings from repositories and owners to gh accounts."""

    def __init__(self, directory: Path | None = None):
        self.directory = (directory or state_dir()).resolve()
        self.path = self.directory / "accounts.json"
        self.lock_path = self.directory / "accounts.lock"

    def load(self) -> dict[str, dict[str, str]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read account bindings {self.path}: {exc}") from exc
        hosts = value.get("hosts") if isinstance(value, dict) else None
        if not isinstance(hosts, dict):
            return {}
        return {
            host: {key: login for key, login in bindings.items() if isinstance(login, str)}
            for host, bindings in hosts.items()
            if isinstance(bindings, dict)
        }

    def lookup(self, hostname: str, repository: str) -> Account | None:
        bindings = self.load().get(hostname, {})
        login = bindings.get(repository_key(repository))
        if login:
            return Account(hostname, login, SOURCE_REPOSITORY)
        login = bindings.get(owner_key(repository.split("/", 1)[0]))
        if login:
            return Account(hostname, login, SOURCE_OWNER)
        return None

    def bind(self, hostname: str, key: str, login: str) -> None:
        self._update(lambda bindings: bindings.__setitem__(key, login), hostname)

    def unbind(self, hostname: str, key: str) -> bool:
        removed: list[bool] = []

        def remove(bindings: dict[str, str]) -> None:
            removed.append(bindings.pop(key, None) is not None)

        self._update(remove, hostname)
        return bool(removed and removed[0])

    def _update(self, change: Callable[[dict[str, str]], None], hostname: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(self.lock_path):
            hosts = self.load()
            bindings = hosts.setdefault(hostname, {})
            change(bindings)
            if not bindings:
                hosts.pop(hostname, None)
            payload = {"schema_version": ACCOUNTS_SCHEMA_VERSION, "hosts": hosts}
            descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".accounts.")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, sort_keys=True)
                    stream.write("\n")
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise


def _ambient_runner(hostname: str) -> Runner:
    # Reading gh's own account list must not be steered by a token variable.
    return Runner(env={"GH_HOST": hostname, "GH_TOKEN": None, "GITHUB_TOKEN": None})


def logged_in_accounts(hostname: str, runner: Runner | None = None) -> list[str]:
    """Return the accounts gh holds a working login for on ``hostname``."""

    runner = runner or _ambient_runner(hostname)
    result = runner.run(
        ["gh", "auth", "status", "--hostname", hostname, "--json", "hosts"], check=False
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    entries = (payload.get("hosts") or {}).get(hostname) or []
    return [
        entry["login"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("login") and entry.get("state") == "success"
    ]


def resolve_account(
    config: Config,
    *,
    store: AccountStore | None = None,
    runner: Runner | None = None,
    persist: bool = True,
) -> Account | None:
    """Return the account to act as, or None to use an explicit environment token."""

    requested = os.environ.get(ACCOUNT_ENV, "").strip()
    if requested:
        return Account(config.hostname, requested, SOURCE_ENVIRONMENT)
    store = store or AccountStore()
    bound = store.lookup(config.hostname, config.repository)
    if bound:
        return bound
    if any(os.environ.get(name) for name in TOKEN_ENVS):
        return None
    accounts = logged_in_accounts(config.hostname, runner)
    by_name = {login.lower(): login for login in accounts}
    owners = [config.repository.split("/", 1)[0], config.project_owner]
    choice: Account | None = None
    for owner in owners:
        if owner.lower() in by_name:
            choice = Account(config.hostname, by_name[owner.lower()], SOURCE_OWNER_MATCH)
            break
    if choice is None and len(accounts) == 1:
        choice = Account(config.hostname, accounts[0], SOURCE_ONLY_ACCOUNT)
    if choice is None:
        if not accounts:
            raise RuntimeError(
                f"no gh account is logged in to {config.hostname}; run 'gh auth login'"
            )
        raise RuntimeError(
            f"cannot choose a GitHub account for {config.repository}: "
            f"{', '.join(sorted(accounts))} are logged in and none owns it; "
            f"run 'kanbanlan account use LOGIN' or set {ACCOUNT_ENV}"
        )
    if persist:
        store.bind(config.hostname, repository_key(config.repository), choice.login)
    return choice


def account_token(hostname: str, login: str) -> str:
    result = _ambient_runner(hostname).run(
        ["gh", "auth", "token", "--hostname", hostname, "--user", login], check=False
    )
    token = result.stdout.strip()
    if result.returncode or not token:
        detail = result.stderr.strip() or "no token returned"
        raise RuntimeError(
            f"gh has no usable login for {login} on {hostname} ({detail}); "
            f"run 'gh auth login --hostname {hostname}' as {login}"
        )
    return token


def account_env(hostname: str, token: str) -> dict[str, str | None]:
    return {
        "GH_HOST": hostname,
        "GH_TOKEN": token,
        "GITHUB_TOKEN": None,
        "GH_ENTERPRISE_TOKEN": None,
    }


class AccountRunner(Runner):
    """A runner whose gh commands act as one resolved account.

    Resolution and the token lookup happen on the first gh command, so a
    command that never reaches GitHub never pays for them. Other programs
    (git) run with the environment untouched.
    """

    def __init__(
        self,
        cwd: Path | None,
        config: Config,
        *,
        resolver: Callable[[Config], Account | None] = resolve_account,
        token_lookup: Callable[[str, str], str] = account_token,
    ):
        super().__init__(cwd)
        self.config = config
        self._resolver = resolver
        self._token_lookup = token_lookup
        self._account: Account | None = None
        self._gh_env: dict[str, str | None] | None = None
        self._resolved = False
        # Board reads issue gh commands from several threads at once; only
        # one of them may resolve, so the token is looked up exactly once.
        self._resolve_lock = threading.Lock()

    @property
    def account(self) -> Account | None:
        self._resolve()
        return self._account

    def _resolve(self) -> None:
        with self._resolve_lock:
            if self._resolved:
                return
            account = self._resolver(self.config)
            if account is not None:
                token = self._token_lookup(account.hostname, account.login)
                self._gh_env = account_env(account.hostname, token)
            self._account = account
            self._resolved = True

    def _execute(self, args: list[str], **kwargs: Any) -> CommandResult:
        if not args or Path(args[0]).name != "gh":
            return super()._execute(args, **kwargs)
        self._resolve()
        if self._gh_env is None:
            return super()._execute(args, **kwargs)
        scoped = Runner(self.cwd, env={**(self.env or {}), **self._gh_env})
        return scoped._execute(args, **kwargs)
