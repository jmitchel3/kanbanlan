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
   account is logged in. Otherwise each logged-in account is asked whether
   it can push to the repository: gh's active account wins when it can, and
   otherwise the one account that can. Because that tiebreak follows gh's
   active account at the moment of the first resolution, the result is
   saved as a binding that ``kanbanlan account use`` can change. A
   successful choice is persisted, so its ``gh`` calls are paid once; a
   failed one is retried by the next command.

Anything else is an error naming the accounts that were considered. An explicit
``GH_TOKEN`` or ``GITHUB_TOKEN`` in the environment (CI, a scoped token) is
honored as is when no account was requested, since it already names one
identity.

git commands that reach the remote (fetch, push, ls-remote, ...) authenticate
through a credential helper, which would otherwise answer as gh's active
account. When the resolved account is not the active one, those commands get
the resolved account's token and gh as their credential helper for the host,
in their own process environment only.
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

from kanbanlan.config import Config, state_dir
from kanbanlan.locks import FileLock
from kanbanlan.runner import CommandError, CommandResult, Runner

ACCOUNT_ENV = "KANBANLAN_GITHUB_ACCOUNT"
TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")
ACCOUNTS_SCHEMA_VERSION = 1

SOURCE_ENVIRONMENT = "environment"
SOURCE_REPOSITORY = "repository binding"
SOURCE_OWNER = "owner binding"
SOURCE_OWNER_MATCH = "automatic: owner is a logged-in account"
SOURCE_ONLY_ACCOUNT = "automatic: only logged-in account"
SOURCE_ACTIVE_ACCESS = "automatic: active account can push to the repository"
SOURCE_ONLY_ACCESS = "automatic: only logged-in account that can push to the repository"


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
    can_push: Callable[[str, str, str], bool | None] | None = None,
    active: Callable[[str], str | None] | None = None,
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
        choice = _choose_by_access(
            config, accounts, can_push or can_push_to, active or active_login
        )
    if choice is None:
        raise RuntimeError(
            f"cannot choose a GitHub account for {config.repository}: "
            f"{', '.join(sorted(accounts))} are logged in and none owns or can push to it; "
            f"run 'kanbanlan account use LOGIN' or set {ACCOUNT_ENV}"
        )
    if persist:
        store.bind(config.hostname, repository_key(config.repository), choice.login)
    return choice


PROBE_TIMEOUT_SECONDS = 15.0


def _choose_by_access(
    config: Config,
    accounts: list[str],
    can_push: Callable[[str, str, str], bool | None],
    active: Callable[[str], str | None],
) -> Account | None:
    current = (active(config.hostname) or "").lower()
    ordered = sorted(accounts, key=lambda login: login.lower() != current)
    pushers: list[str] = []
    unknown: list[str] = []
    for login in ordered:
        verdict = can_push(config.hostname, config.repository, login)
        if verdict and login.lower() == current:
            return Account(config.hostname, login, SOURCE_ACTIVE_ACCESS)
        if verdict is None:
            unknown.append(login)
        elif verdict:
            pushers.append(login)
    if unknown:
        raise RuntimeError(
            f"cannot choose a GitHub account for {config.repository}: could not check "
            f"push access for {', '.join(sorted(unknown))}; retry, or run "
            f"'kanbanlan account use LOGIN' or set {ACCOUNT_ENV}"
        )
    if len(pushers) == 1:
        return Account(config.hostname, pushers[0], SOURCE_ONLY_ACCESS)
    if pushers:
        raise RuntimeError(
            f"cannot choose a GitHub account for {config.repository}: "
            f"{', '.join(sorted(pushers))} can all push to it and none is gh's active "
            f"account; run 'kanbanlan account use LOGIN' or set {ACCOUNT_ENV}"
        )
    return None


def can_push_to(
    hostname: str, repository: str, login: str, runner: Runner | None = None
) -> bool | None:
    """Report whether ``login`` can push to ``repository``; None when unknown."""

    try:
        token = account_token(hostname, login)
    except RuntimeError:
        return None
    runner = runner or Runner(env=account_env(hostname, token))
    try:
        result = runner.run(
            ["gh", "api", f"repos/{repository}", "--jq", ".permissions.push"],
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except CommandError:
        return None
    if result.returncode == 0:
        return result.stdout.strip() == "true"
    if "HTTP 404" in result.stderr:
        return False
    return None


# Tokens already looked up by this process; each verb step builds its own runner.
_TOKENS: dict[tuple[str, str], str] = {}


def account_token(hostname: str, login: str) -> str:
    cached = _TOKENS.get((hostname, login))
    if cached:
        return cached
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
    _TOKENS[(hostname, login)] = token
    return token


def active_login(hostname: str) -> str | None:
    """Return gh's active account on ``hostname``, read from its config file."""

    result = _ambient_runner(hostname).run(
        ["gh", "config", "get", "user", "--host", hostname], check=False
    )
    login = result.stdout.strip()
    return login if result.returncode == 0 and login else None


# git subcommands that talk to a remote, so need credentials.
GIT_REMOTE_SUBCOMMANDS = frozenset({"fetch", "push", "pull", "ls-remote", "clone"})
GIT_REMOTE_REMOTE_ACTIONS = frozenset({"update", "prune", "show", "set-head"})
# git global options that take a separate value.
_GIT_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)


def git_reaches_remote(args: list[str]) -> bool:
    """Report whether a git command line contacts a remote."""

    if not args or Path(args[0]).name != "git":
        return False
    index = 1
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in _GIT_VALUE_OPTIONS else 1
    if index >= len(args):
        return False
    subcommand = args[index]
    if subcommand in GIT_REMOTE_SUBCOMMANDS:
        return True
    if subcommand == "remote":
        return index + 1 < len(args) and args[index + 1] in GIT_REMOTE_REMOTE_ACTIONS
    if subcommand == "submodule":
        return index + 1 < len(args) and args[index + 1] in {"update", "sync", "add"}
    return False


def git_account_env(hostname: str, token: str) -> dict[str, str | None]:
    """Environment that makes git authenticate to ``hostname`` with ``token``.

    gh's credential helper answers with ``GH_TOKEN``. Configuring it through
    ``GIT_CONFIG_*`` (after an empty value that clears any helper from git
    config, such as a keychain) keeps the token out of git config and argv.
    """

    try:
        count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        count = 0
    key = f"credential.https://{hostname}.helper"
    env = account_env(hostname, token)
    for offset, value in enumerate(("", "!gh auth git-credential")):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + 2)
    return env


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "[redacted]") if secret and text else text


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
    command that never reaches GitHub never pays for them. git commands that
    reach a remote authenticate as the same account when it is not gh's
    active one (see ``git_account_env``); other programs, and every other
    git command, run with the environment untouched.
    """

    def __init__(
        self,
        cwd: Path | None,
        config: Config,
        *,
        resolver: Callable[[Config], Account | None] = resolve_account,
        token_lookup: Callable[[str, str], str] = account_token,
        active_lookup: Callable[[str], str | None] = active_login,
    ):
        super().__init__(cwd)
        self.config = config
        self._resolver = resolver
        self._token_lookup = token_lookup
        self._active_lookup = active_lookup
        self._token: str | None = None
        self._git_env: dict[str, str | None] | None = None
        self._git_resolved = False
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
                self._token = token
                self._gh_env = account_env(account.hostname, token)
            self._account = account
            self._resolved = True

    def _resolve_git(self) -> dict[str, str | None] | None:
        """Return the environment remote git commands need, or None for none."""

        try:
            self._resolve()
        except RuntimeError:
            # gh commands report an unresolvable account; git keeps its own auth.
            return None
        with self._resolve_lock:
            if not self._git_resolved:
                account = self._account
                if account is not None and self._token:
                    ambient_token = any(os.environ.get(name) for name in TOKEN_ENVS)
                    active = None if ambient_token else self._active_lookup(account.hostname)
                    if ambient_token or (active or "").lower() != account.login.lower():
                        self._git_env = git_account_env(account.hostname, self._token)
                self._git_resolved = True
            return self._git_env

    def _execute(self, args: list[str], **kwargs: Any) -> CommandResult:
        if git_reaches_remote(args):
            git_env = self._resolve_git()
            if git_env is None:
                return super()._execute(args, **kwargs)
            scoped = Runner(self.cwd, env={**(self.env or {}), **git_env})
            return self._redacted(scoped, args, **kwargs)
        if not args or Path(args[0]).name != "gh":
            return super()._execute(args, **kwargs)
        self._resolve()
        if self._gh_env is None:
            return super()._execute(args, **kwargs)
        scoped = Runner(self.cwd, env={**(self.env or {}), **self._gh_env})
        return scoped._execute(args, **kwargs)

    def _redacted(self, runner: Runner, args: list[str], **kwargs: Any) -> CommandResult:
        token = self._token or ""
        try:
            result = runner._execute(args, **kwargs)
        except CommandError as exc:
            raise CommandError(_redact_result(exc.result, token)) from None
        return _redact_result(result, token)


def _redact_result(result: CommandResult, token: str) -> CommandResult:
    return CommandResult(
        args=tuple(redact(value, token) for value in result.args),
        returncode=result.returncode,
        stdout=redact(result.stdout, token),
        stderr=redact(result.stderr, token),
    )
