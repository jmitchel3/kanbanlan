from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.registry import Registration, RegistryStore
from kanbanlan.worker import (
    Worker,
    WorkerAlreadyRunning,
    WorkerLock,
    scoped_runner,
    token_env_name,
    worker_status,
)


class WorkerTests(unittest.TestCase):
    def test_process_lock_rejects_a_live_pid_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "worker.lock"
            with WorkerLock(lock_path):
                with self.assertRaises(WorkerAlreadyRunning):
                    with WorkerLock(lock_path):
                        pass
            self.assertFalse(lock_path.exists())

    def test_disabled_repository_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            common = Path(directory) / "common"
            store.register(
                common_dir=common,
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            store.disable(common)
            result = Worker(store).run_once()
            self.assertEqual(0, result["attempted"])
            self.assertEqual(1, result["skipped"])

    def test_scoped_runner_uses_token_env_without_switching_accounts(self) -> None:
        registration = Registration(
            common_dir="/tmp/common",
            root="/tmp/root",
            repository="acme/one",
            hostname="github.com",
            github_login="alice",
        )
        token_name = token_env_name("github.com", "alice")
        with mock.patch.dict(os.environ, {token_name: "secret"}, clear=False):
            runner = scoped_runner(registration)
        self.assertEqual("secret", runner.env["GH_TOKEN"])
        self.assertEqual("github.com", runner.env["GH_HOST"])
        self.assertNotIn("gh auth switch", runner.env)

    def test_successful_iteration_refreshes_plans_and_resets_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            registration = store.register(
                common_dir=Path(directory) / "common",
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            registration.consecutive_failures = 2
            registration.next_retry_at = None
            store.update(registration)
            provider = mock.Mock()
            provider.list_open_requests.return_value = []
            cache = mock.Mock()
            cache.refresh.return_value = {"items": []}
            with (
                mock.patch("kanbanlan.worker.Config.load"),
                mock.patch("kanbanlan.worker.scoped_runner", return_value=mock.Mock()),
                mock.patch("kanbanlan.worker.GitHub", return_value=provider),
                mock.patch("kanbanlan.worker.cache_dir", return_value=Path(directory) / "cache"),
                mock.patch("kanbanlan.worker.CacheStore", return_value=cache),
                mock.patch("kanbanlan.worker.plan_reconciliation", return_value=[]),
            ):
                result = Worker(store).run_once()

            self.assertEqual(1, result["succeeded"])
            updated = store.registrations()[0]
            self.assertEqual(0, updated.consecutive_failures)
            self.assertIsNotNone(updated.last_success_at)
            cache.refresh.assert_called_once_with(provider)

    def test_failed_iteration_records_bounded_backoff_and_keeps_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            store.register(
                common_dir=Path(directory) / "common",
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            with (
                mock.patch("kanbanlan.worker.Config.load", side_effect=RuntimeError("bad config")),
            ):
                result = Worker(store).run_once()

            self.assertEqual(1, result["failed"])
            updated = store.registrations()[0]
            self.assertEqual(1, updated.consecutive_failures)
            self.assertEqual("RuntimeError", updated.last_error["kind"])
            self.assertIsNotNone(updated.next_retry_at)

    def test_status_reports_registry_and_worker_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            payload = worker_status(store)
            self.assertFalse(payload["worker"]["running"])
            self.assertEqual([], payload["repositories"])
