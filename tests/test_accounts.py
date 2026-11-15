from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.accounts import (
    ACCOUNT_ENV,
    SOURCE_ENVIRONMENT,
    SOURCE_ONLY_ACCOUNT,
    SOURCE_OWNER,
    SOURCE_OWNER_MATCH,
    SOURCE_REPOSITORY,
    Account,
    AccountRunner,
    AccountStore,
    git_account_env,
    git_reaches_remote,
    owner_key,
    repository_key,
    resolve_account,
)
from kanbanlan.config import Config
from kanbanlan.runner import CommandError, CommandResult, Runner


def config(repository: str = "acme/widget", project_owner: str = "acme") -> Config:
    return Config(
        repository=repository,
        project_owner=project_owner,
        project_owner_type="organization",
        project_number=2,
    )


class StatusRunner:
    """Stands in for ``gh auth status --json hosts``."""

    def __init__(self, *logins: str, failed: tuple[str, ...] = ()):
        entries = [{"login": login, "state": "success"} for login in logins]
        entries += [{"login": login, "state": "error"} for login in failed]
        self.payload = json.dumps({"hosts": {"github.com": entries}})
        self.calls = 0

    def run(self, args, **_kwargs):
        self.calls += 1
        return CommandResult(tuple(args), 0, self.payload, "")


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self.directory.name))
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (ACCOUNT_ENV, "GH_TOKEN", "GITHUB_TOKEN"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def resolve(self, runner: StatusRunner, **kwargs) -> Account | None:
        return resolve_account(
            kwargs.pop("cfg", config()), store=self.store, runner=runner, **kwargs
        )

    def test_environment_account_wins_over_every_binding(self) -> None:
        self.store.bind("github.com", repository_key("acme/widget"), "bound")
        os.environ[ACCOUNT_ENV] = "chosen"

        account = self.resolve(StatusRunner("acme"))

        self.assertEqual(("chosen", SOURCE_ENVIRONMENT), (account.login, account.source))

    def test_repository_binding_wins_over_owner_binding(self) -> None:
        self.store.bind("github.com", owner_key("acme"), "org-bot")
        self.store.bind("github.com", repository_key("acme/widget"), "widget-bot")

        account = self.resolve(StatusRunner())

        self.assertEqual(("widget-bot", SOURCE_REPOSITORY), (account.login, account.source))

    def test_owner_binding_covers_every_repository_of_that_owner(self) -> None:
        self.store.bind("github.com", owner_key("acme"), "org-bot")

        account = self.resolve(StatusRunner(), cfg=config("ACME/other"))

        self.assertEqual(("org-bot", SOURCE_OWNER), (account.login, account.source))

    def test_a_binding_never_consults_gh(self) -> None:
        self.store.bind("github.com", repository_key("acme/widget"), "widget-bot")
        runner = StatusRunner()

        self.resolve(runner)

        self.assertEqual(0, runner.calls)

    def test_the_owner_account_is_chosen_over_the_active_one_and_persisted(self) -> None:
        runner = StatusRunner("someone-else", "acme")

        account = self.resolve(runner)
        again = self.resolve(runner)

        self.assertEqual(("acme", SOURCE_OWNER_MATCH), (account.login, account.source))
        self.assertEqual(("acme", SOURCE_REPOSITORY), (again.login, again.source))
        self.assertEqual(1, runner.calls)

    def test_the_project_owner_qualifies_when_the_repository_owner_is_not_logged_in(self) -> None:
        account = self.resolve(
            StatusRunner("other", "boardowner"), cfg=config(project_owner="boardowner")
        )

        self.assertEqual("boardowner", account.login)

    def test_a_single_logged_in_account_is_chosen(self) -> None:
        account = self.resolve(StatusRunner("solo", failed=("broken",)))

        self.assertEqual(("solo", SOURCE_ONLY_ACCOUNT), (account.login, account.source))

    def test_an_ambiguous_choice_is_an_error_naming_the_accounts(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "alpha, beta are logged in.*account use"):
            self.resolve(StatusRunner("beta", "alpha"))
        self.assertIsNone(self.store.lookup("github.com", "acme/widget"))

    def test_no_logged_in_account_is_an_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "gh auth login"):
            self.resolve(StatusRunner())

    def test_an_environment_token_is_honored_when_nothing_is_bound(self) -> None:
        os.environ["GH_TOKEN"] = "ci-token"
        runner = StatusRunner("alpha", "beta")

        self.assertIsNone(self.resolve(runner))
        self.assertEqual(0, runner.calls)

    def test_a_binding_still_wins_over_an_environment_token(self) -> None:
        os.environ["GH_TOKEN"] = "ci-token"
        self.store.bind("github.com", repository_key("acme/widget"), "widget-bot")

        self.assertEqual("widget-bot", self.resolve(StatusRunner()).login)

    def test_show_does_not_persist_an_automatic_choice(self) -> None:
        self.resolve(StatusRunner("acme"), persist=False)

        self.assertIsNone(self.store.lookup("github.com", "acme/widget"))

    def test_unbind_removes_only_the_named_binding(self) -> None:
        self.store.bind("github.com", owner_key("acme"), "org-bot")
        self.store.bind("github.com", repository_key("acme/widget"), "widget-bot")

        self.assertTrue(self.store.unbind("github.com", repository_key("acme/widget")))
        self.assertFalse(self.store.unbind("github.com", repository_key("acme/widget")))
        self.assertEqual("org-bot", self.store.lookup("github.com", "acme/widget").login)
        self.assertEqual(0o600, os.stat(self.store.path).st_mode & 0o777)


class AccountRunnerTests(unittest.TestCase):
    def runner(self, resolver=None, tokens=None) -> AccountRunner:
        return AccountRunner(
            Path("/tmp"),
            config(),
            resolver=resolver or (lambda _config: Account("github.com", "acme", "test")),
            token_lookup=tokens or (lambda host, login: f"token-for-{login}"),
        )

    def test_gh_commands_carry_the_bound_token_and_drop_ambient_ones(self) -> None:
        runner = self.runner()
        with mock.patch("kanbanlan.runner.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            runner.run(["gh", "api", "user"])

        env = run.call_args.kwargs["env"]
        self.assertEqual("token-for-acme", env["GH_TOKEN"])
        self.assertEqual("github.com", env["GH_HOST"])
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_other_programs_run_with_the_environment_untouched(self) -> None:
        resolver = mock.Mock()
        runner = self.runner(resolver=resolver)
        with mock.patch("kanbanlan.runner.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            runner.run(["git", "status"])

        self.assertIsNone(run.call_args.kwargs["env"])
        resolver.assert_not_called()

    def test_concurrent_gh_commands_resolve_the_account_once(self) -> None:
        lookups: list[str] = []
        gate = threading.Barrier(4, timeout=5)

        def tokens(host, login):
            lookups.append(login)
            return "token"

        runner = self.runner(tokens=tokens)

        def call() -> None:
            gate.wait()
            runner.run(["gh", "api", "user"])

        with mock.patch("kanbanlan.runner.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            threads = [threading.Thread(target=call) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

        self.assertEqual(["acme"], lookups)
        self.assertEqual(4, run.call_count)

    def test_an_unresolved_account_leaves_gh_to_the_environment_token(self) -> None:
        runner = self.runner(resolver=lambda _config: None)
        with mock.patch("kanbanlan.runner.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            runner.run(["gh", "api", "user"])

        self.assertIsNone(run.call_args.kwargs["env"])


FAKE_GIT = """#!/bin/sh
{
  echo "args=$*"
  echo "GH_TOKEN=${GH_TOKEN-<unset>}"
  echo "GITHUB_TOKEN=${GITHUB_TOKEN-<unset>}"
  echo "GIT_CONFIG_COUNT=${GIT_CONFIG_COUNT-<unset>}"
  i=0
  while [ "$i" -lt "${GIT_CONFIG_COUNT:-0}" ]; do
    eval "echo GIT_CONFIG_KEY_$i=\\$GIT_CONFIG_KEY_$i"
    eval "echo GIT_CONFIG_VALUE_$i=\\$GIT_CONFIG_VALUE_$i"
    i=$((i + 1))
  done
} >> "$FAKE_GIT_LOG"
if [ -n "$FAKE_GIT_FAIL" ]; then
  echo "remote: Permission denied (token $GH_TOKEN)" >&2
  exit 1
fi
"""

# Stands in for gh's credential helper: answers with whatever GH_TOKEN it got.
FAKE_GH = """#!/bin/sh
if [ "$1 $2 $3" = "auth git-credential get" ]; then
  cat >/dev/null
  echo "username=x-access-token"
  echo "password=${GH_TOKEN-none}"
fi
"""


class RemoteGitTests(unittest.TestCase):
    """git commands that reach the remote authenticate as the resolved account."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("git", FAKE_GIT), ("gh", FAKE_GH)):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.log = self.root / "git.log"
        patcher = mock.patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GIT_LOG": str(self.log)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (ACCOUNT_ENV, "GH_TOKEN", "GITHUB_TOKEN", "GIT_CONFIG_COUNT", "FAKE_GIT_FAIL"):
            os.environ.pop(name, None)
        self.store = AccountStore(self.root / "state")

    def runner(self, *, bound="acme", active="j-other", resolver=None) -> AccountRunner:
        self.token_lookups: list[str] = []

        def tokens(host, login):
            self.token_lookups.append(login)
            return f"secret-token-for-{login}"

        return AccountRunner(
            self.root,
            config(),
            resolver=resolver or (lambda _config: Account("github.com", bound, "test")),
            token_lookup=tokens,
            active_lookup=lambda _host: active,
        )

    def recorded(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self.log.read_text().splitlines():
            name, _, value = line.partition("=")
            values[name] = value
        return values

    def assert_scoped(self, token: str) -> None:
        values = self.recorded()
        self.assertEqual(token, values["GH_TOKEN"])
        self.assertEqual("<unset>", values["GITHUB_TOKEN"])
        self.assertEqual("2", values["GIT_CONFIG_COUNT"])
        self.assertEqual("credential.https://github.com.helper", values["GIT_CONFIG_KEY_0"])
        self.assertEqual("", values["GIT_CONFIG_VALUE_0"])
        self.assertEqual("!gh auth git-credential", values["GIT_CONFIG_VALUE_1"])

    def assert_untouched(self) -> None:
        values = self.recorded()
        self.assertEqual("<unset>", values["GH_TOKEN"])
        self.assertEqual("<unset>", values["GIT_CONFIG_COUNT"])

    def test_push_authenticates_as_the_bound_account_when_another_is_active(self) -> None:
        self.runner().run(["git", "push", "--set-upstream", "origin", "work/x"])

        self.assert_scoped("secret-token-for-acme")

    def test_fetch_ls_remote_and_option_prefixed_commands_are_scoped(self) -> None:
        for command in (
            ["git", "fetch", "origin", "main"],
            ["git", "ls-remote", "origin"],
            ["git", "-C", str(self.root), "fetch", "origin"],
            ["git", "-c", "core.askPass=", "pull"],
            ["git", "remote", "update"],
        ):
            with self.subTest(command=command):
                self.log.unlink(missing_ok=True)
                self.runner().run(command)
                self.assert_scoped("secret-token-for-acme")

    def test_local_git_commands_run_untouched_and_never_resolve(self) -> None:
        resolver = mock.Mock()
        runner = self.runner(resolver=resolver)
        for command in (
            ["git", "status"],
            ["git", "worktree", "add", "-b", "work/x", "/tmp/x", "origin/main"],
            ["git", "remote", "get-url", "origin"],
            ["git", "-C", "fetch", "status"],
        ):
            runner.run(command)
        resolver.assert_not_called()
        self.assertNotIn("GH_TOKEN=secret", self.log.read_text())

    def test_the_bound_account_being_active_leaves_git_untouched(self) -> None:
        self.runner(bound="acme", active="Acme").run(["git", "push", "origin", "work/x"])

        self.assert_untouched()

    def test_no_resolved_account_leaves_git_untouched(self) -> None:
        self.runner(resolver=lambda _config: None).run(["git", "fetch", "origin"])

        self.assert_untouched()

    def test_an_unresolvable_account_leaves_git_to_its_own_credentials(self) -> None:
        def ambiguous(_config):
            raise RuntimeError("cannot choose a GitHub account")

        self.runner(resolver=ambiguous).run(["git", "fetch", "origin"])

        self.assert_untouched()

    def test_the_environment_account_is_used_for_git(self) -> None:
        os.environ[ACCOUNT_ENV] = "acme-bot"
        runner = self.runner(
            resolver=lambda value: resolve_account(value, store=self.store, persist=False)
        )

        runner.run(["git", "push", "origin", "work/x"])

        self.assert_scoped("secret-token-for-acme-bot")
        self.assertEqual(["acme-bot"], self.token_lookups)

    def test_an_ambient_token_is_replaced_for_a_bound_account(self) -> None:
        os.environ["GH_TOKEN"] = "ambient-token"
        os.environ["GITHUB_TOKEN"] = "ambient-token"

        self.runner(active="acme").run(["git", "fetch", "origin"])

        self.assert_scoped("secret-token-for-acme")

    def test_existing_git_config_environment_is_extended_not_replaced(self) -> None:
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "user.name"
        os.environ["GIT_CONFIG_VALUE_0"] = "Someone"

        self.runner().run(["git", "fetch", "origin"])

        values = self.recorded()
        self.assertEqual("3", values["GIT_CONFIG_COUNT"])
        self.assertEqual("user.name", values["GIT_CONFIG_KEY_0"])
        self.assertEqual("credential.https://github.com.helper", values["GIT_CONFIG_KEY_1"])
        self.assertEqual("!gh auth git-credential", values["GIT_CONFIG_VALUE_2"])

    def test_a_failure_never_reveals_the_token(self) -> None:
        os.environ["FAKE_GIT_FAIL"] = "1"

        with self.assertRaises(CommandError) as raised:
            self.runner().run(["git", "push", "origin", "work/x"])

        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("secret-token", raised.exception.result.stderr)
        self.assertIn("[redacted]", str(raised.exception))
        result = self.runner().run(["git", "push", "origin", "work/x"], check=False)
        self.assertNotIn("secret-token", result.stderr)

    def test_the_token_is_never_written_to_git_config(self) -> None:
        self.runner().run(["git", "push", "origin", "work/x"])

        self.assertNotIn("secret-token", "".join(self.recorded()["args"]))
        self.assertFalse((self.root / ".git").exists())


class GitCredentialEnvironmentTests(unittest.TestCase):
    """Real git asks gh, with the bound token, even over a configured helper."""

    def test_real_git_resolves_credentials_through_gh_with_the_token(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(FAKE_GH)
            gh.chmod(0o755)
            global_config = root / "gitconfig"
            # The keychain-style helper the environment must override.
            global_config.write_text(
                '[credential]\n\thelper = "!f() { echo password=wrong-account; }; f"\n'
            )
            env = {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                **git_account_env("github.com", "bound-token"),
            }
            result = Runner(root, env=env).run(
                ["git", "credential", "fill"],
                input_text="protocol=https\nhost=github.com\n\n",
            )

        self.assertIn("password=bound-token", result.stdout)
        self.assertNotIn("wrong-account", result.stdout)


class GitRemoteDetectionTests(unittest.TestCase):
    def test_only_commands_that_contact_a_remote_qualify(self) -> None:
        self.assertTrue(git_reaches_remote(["git", "push"]))
        self.assertTrue(git_reaches_remote(["/usr/bin/git", "--no-pager", "fetch"]))
        self.assertTrue(git_reaches_remote(["git", "-C", "/x", "ls-remote", "origin"]))
        self.assertTrue(git_reaches_remote(["git", "submodule", "update", "--init"]))
        self.assertFalse(git_reaches_remote(["git", "worktree", "add", "x", "origin/main"]))
        self.assertFalse(git_reaches_remote(["git", "remote", "get-url", "origin"]))
        self.assertFalse(git_reaches_remote(["git"]))
        self.assertFalse(git_reaches_remote(["gh", "fetch"]))


class StartFetchTests(unittest.TestCase):
    """start fetches the default branch as the bound account."""

    def test_start_fetches_through_the_account_runner(self) -> None:
        from kanbanlan.cli import lifecycle

        runner = mock.Mock()
        runner.run.return_value = CommandResult(("git",), 0, "", "")
        with mock.patch.object(lifecycle, "AccountRunner", return_value=runner) as factory:
            self.assertTrue(lifecycle._fetch_default_branch(Path("/repo"), config()))
            lifecycle._create_worktree(Path("/repo"), config(), "work/x", Path("/repo/.w/x"))

        self.assertEqual(2, factory.call_count)
        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertEqual(["git", "fetch", "origin", "main"], commands[0])
        self.assertIn(["git", "fetch", "origin", "main"], commands[1:])
