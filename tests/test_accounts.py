from __future__ import annotations

import json
import os
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
    owner_key,
    repository_key,
    resolve_account,
)
from kanbanlan.config import Config
from kanbanlan.runner import CommandResult


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
