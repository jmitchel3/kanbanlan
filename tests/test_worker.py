from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from kanbanlan.accounts import AccountStore, repository_key
from kanbanlan.config import Config
from kanbanlan.registry import Registration, RegistryStore, utc_now
from kanbanlan.runner import CommandResult, RateLimitError
from kanbanlan.snapshot import SCHEMA_VERSION, CacheStore, isoformat
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
    def test_quota_cooldown_is_shared_persisted_and_expires(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = RegistryStore(root / "state")
            config = Config("acme/one", "acme", "organization", 2)
            now = datetime.now(UTC)
            reset = now + timedelta(minutes=20)
            for name, login, hostname in (
                ("a", "alice", "github.com"),
                ("b", "alice", "github.com"),
                ("c", "bob", "github.com"),
                ("d", "alice", "git.example.com"),
            ):
                registry.register(
                    common_dir=root / name / ".git",
                    root=root / name,
                    repository=f"acme/{name}",
                    hostname=hostname,
                    github_login=login,
                )
                cache = CacheStore(config, root / name / "cache")
                cache._write_json(
                    cache.snapshot_path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "generated_at": isoformat(now),
                        "rate_limit": {
                            "remaining": 10 if name == "a" else 4000,
                            "resetAt": isoformat(reset),
                        },
                    },
                )
            with (
                mock.patch("kanbanlan.worker.Config.load", return_value=config),
                mock.patch("kanbanlan.worker.scoped_runner"),
                mock.patch("kanbanlan.worker.GitHub"),
                mock.patch("kanbanlan.worker.cache_dir", side_effect=lambda p: p / "cache"),
                mock.patch("kanbanlan.worker.drain_outbox") as drain,
                mock.patch("kanbanlan.worker.read_board", return_value=({"items": []}, [])) as read,
            ):
                first = Worker(registry).run_once()
                self.assertEqual(1, first["failed"])
                self.assertEqual(1, first["skipped"])
                self.assertEqual(2, first["succeeded"])
                limited = registry.get(str(root / "a" / ".git"))
                self.assertEqual(isoformat(reset), limited.next_retry_at)
                self.assertEqual("github.com:alice", limited.last_error["rate_limit_account"])
                self.assertEqual(2, read.call_count)
                self.assertEqual(2, drain.call_count)

                # A new Worker sees the persisted account cooldown before it
                # considers b, which has never itself made a failed request.
                second = Worker(registry).run_once()
                self.assertEqual(0, second["attempted"])
                self.assertEqual(4, second["skipped"])

                after_reset = reset + timedelta(seconds=1)
                with (
                    mock.patch("kanbanlan.worker.datetime", wraps=datetime) as clock,
                    mock.patch("kanbanlan.snapshot.utc_now", return_value=after_reset),
                ):
                    clock.now.return_value = after_reset
                    third = Worker(registry).run_once()
                self.assertEqual(4, third["succeeded"])
                self.assertIsNone(registry.get(str(root / "a" / ".git")).last_error)

    def test_account_binding_controls_shared_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = RegistryStore(root)
            first = registry.register(
                common_dir=root / "a",
                root=root,
                repository="acme/a",
                hostname="github.com",
                github_login="alice",
            )
            first.last_error = {"kind": "RateLimitError", "rate_limit_account": "github.com:alice"}
            first.next_retry_at = isoformat(datetime.now(UTC) + timedelta(minutes=20))
            registry.update(first)
            for name in ("b", "c"):
                registry.register(
                    common_dir=root / name,
                    root=root,
                    repository=f"acme/{name}",
                    hostname="github.com",
                    github_login="alice",
                )
            AccountStore().bind("github.com", repository_key("acme/b"), "bob")
            worker = Worker(registry)
            with mock.patch.object(worker, "_run_registration") as run:
                result = worker.run_once()
            self.assertEqual(1, result["attempted"])
            self.assertEqual("acme/b", run.call_args.args[0].repository)

    def test_rate_limit_without_reset_uses_polling_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = RegistryStore(Path(directory))
            registry.register(
                common_dir=Path(directory) / "common",
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            now = datetime.now(UTC)
            with mock.patch("kanbanlan.worker.Config.load", side_effect=RateLimitError("limited")):
                result = Worker(registry).run_once()
            self.assertEqual(1, result["failed"])
            retry = datetime.fromisoformat(registry.registrations()[0].next_retry_at)
            self.assertGreaterEqual((retry - now).total_seconds(), 300)

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
            mock.patch("kanbanlan.accounts.Runner", return_value=token_runner) as lookup,
            mock.patch("kanbanlan.worker.Runner", return_value=scoped) as runner,
        ):
            result = scoped_runner(registration)

        self.assertIs(scoped, result)
        token_lookup_env = lookup.call_args.kwargs["env"]
        self.assertIsNone(token_lookup_env["GH_TOKEN"])
        self.assertIsNone(token_lookup_env["GITHUB_TOKEN"])
        self.assertIn("alice", token_runner.run.call_args.args[0])
        self.assertEqual("selected", runner.call_args.kwargs["env"]["GH_TOKEN"])

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
                mock.patch("kanbanlan.worker.drain_outbox"),
                mock.patch("kanbanlan.worker.plan_reconciliation", return_value=[]),
            ):
                result = Worker(store).run_once()

            self.assertEqual(1, result["succeeded"])
            updated = store.registrations()[0]
            self.assertEqual(0, updated.consecutive_failures)
            self.assertIsNotNone(updated.last_success_at)
            # A clean cycle pays for exactly one refresh and one issue sweep;
            # verification only re-reads live state after a repair.
            self.assertEqual([mock.call(provider)], cache.refresh.call_args_list)
            provider.list_open_requests.assert_called_once()

    def test_applied_repair_is_verified_with_a_second_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            store.register(
                common_dir=Path(directory) / "common",
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            provider = mock.Mock()
            provider.list_open_requests.return_value = []
            cache = mock.Mock()
            cache.refresh.return_value = {"items": []}
            drift = mock.Mock(kind="missing_projection")
            with (
                mock.patch("kanbanlan.worker.Config.load"),
                mock.patch("kanbanlan.worker.scoped_runner", return_value=mock.Mock()),
                mock.patch("kanbanlan.worker.GitHub", return_value=provider),
                mock.patch("kanbanlan.worker.cache_dir", return_value=Path(directory) / "cache"),
                mock.patch("kanbanlan.worker.CacheStore", return_value=cache),
                mock.patch("kanbanlan.worker.drain_outbox"),
                mock.patch(
                    "kanbanlan.worker.plan_reconciliation",
                    side_effect=[[drift], []],
                ),
                mock.patch(
                    "kanbanlan.worker.apply_reconciliation",
                    return_value=([], []),
                ) as apply_mock,
            ):
                result = Worker(store).run_once()

            self.assertEqual(1, result["succeeded"])
            apply_mock.assert_called_once()
            self.assertEqual(
                [mock.call(provider), mock.call(provider)], cache.refresh.call_args_list
            )
            self.assertEqual(2, provider.list_open_requests.call_count)

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
