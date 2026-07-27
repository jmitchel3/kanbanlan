from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.registry import Registration, RegistryStore, utc_now
from kanbanlan.runner import CommandResult
from kanbanlan.worker import (
    Worker,
    WorkerAlreadyRunning,
    WorkerLock,
    scoped_runner,
    start_worker,
    stop_worker,
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

    def test_process_lock_atomically_replaces_a_dead_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "worker.lock"
            lock_path.write_text('{"pid": 999999, "started_at": "earlier"}\n', encoding="utf-8")
            with mock.patch("kanbanlan.worker._pid_running", return_value=False):
                with WorkerLock(lock_path):
                    self.assertEqual(os.getpid(), int(json.loads(lock_path.read_text())["pid"]))
            self.assertFalse(lock_path.exists())

    def test_forever_worker_holds_one_lock_across_sleep_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            lock_path = Path(directory) / "worker.lock"

            def stop_after_first_iteration(_seconds: float) -> None:
                self.assertTrue(lock_path.exists())
                with self.assertRaises(WorkerAlreadyRunning):
                    with WorkerLock(lock_path):
                        pass
                raise StopIteration

            with self.assertRaises(StopIteration):
                Worker(store, sleep=stop_after_first_iteration).run_forever()

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

    def test_scoped_runner_removes_ambient_tokens_when_loading_selected_account(self) -> None:
        registration = Registration(
            common_dir="/tmp/common",
            root="/tmp/root",
            repository="acme/one",
            hostname="github.com",
            github_login="alice",
        )
        token_runner = mock.Mock()
        token_runner.run.return_value = CommandResult(("gh", "auth", "token"), 0, "selected\n", "")
        scoped = mock.Mock()
        with (
            mock.patch.dict(os.environ, {"GH_TOKEN": "ambient"}, clear=False),
            mock.patch("kanbanlan.worker.Runner", side_effect=[token_runner, scoped]) as runner,
        ):
            result = scoped_runner(registration)

        self.assertIs(scoped, result)
        token_lookup_env = runner.call_args_list[0].kwargs["env"]
        self.assertIsNone(token_lookup_env["GH_TOKEN"])
        self.assertIsNone(token_lookup_env["GITHUB_TOKEN"])
        self.assertEqual("selected", runner.call_args_list[1].kwargs["env"]["GH_TOKEN"])

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
            self.assertEqual(
                [mock.call(provider), mock.call(provider)], cache.refresh.call_args_list
            )

    def test_recently_serviced_repository_waits_for_its_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            registration = store.register(
                common_dir=Path(directory) / "common",
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
                interval_seconds=60,
            )
            registration.last_run_at = utc_now()
            store.update(registration)

            result = Worker(store).run_once()

            self.assertEqual(0, result["attempted"])
            self.assertEqual(1, result["skipped"])

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

    def test_status_reports_the_live_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            with WorkerLock(Path(directory) / "worker.lock"):
                payload = worker_status(store)
            self.assertTrue(payload["worker"]["running"])
            self.assertEqual(os.getpid(), payload["worker"]["pid"])

    def test_start_waits_until_child_owns_the_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            stopped = {"worker": {"pid": None, "running": False}, "repositories": []}
            running = {"worker": {"pid": 123, "running": True}, "repositories": []}
            process = mock.Mock(pid=123)
            with (
                mock.patch("kanbanlan.worker.worker_status", side_effect=[stopped, running]),
                mock.patch("kanbanlan.worker.subprocess.Popen", return_value=process) as popen,
            ):
                payload = start_worker(store, interval_seconds=60)

            self.assertEqual(running, payload)
            self.assertEqual("-m", popen.call_args.args[0][1])
            self.assertIn("--interval", popen.call_args.args[0])

    def test_concurrent_start_returns_existing_owner_and_stops_extra_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            stopped = {"worker": {"pid": None, "running": False}, "repositories": []}
            running = {"worker": {"pid": 456, "running": True}, "repositories": []}
            process = mock.Mock(pid=123)
            process.poll.return_value = None
            with (
                mock.patch("kanbanlan.worker.worker_status", side_effect=[stopped, running]),
                mock.patch("kanbanlan.worker.subprocess.Popen", return_value=process),
            ):
                payload = start_worker(store)

            self.assertEqual(running, payload)
            process.terminate.assert_called_once_with()

    def test_stop_waits_for_the_locked_process_to_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            running = {"worker": {"pid": 123, "running": True}, "repositories": []}
            stopped = {"worker": {"pid": None, "running": False}, "repositories": []}
            with (
                mock.patch("kanbanlan.worker.worker_status", side_effect=[running, stopped]),
                mock.patch("kanbanlan.worker._pid_running", return_value=False),
                mock.patch("kanbanlan.worker.os.kill") as kill,
            ):
                payload = stop_worker(store)

            kill.assert_called_once_with(123, signal.SIGTERM)
            self.assertEqual(stopped, payload)
