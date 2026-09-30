from __future__ import annotations

import json
import os
import signal
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from kanbanlan.accounts import AccountStore, repository_key
from kanbanlan.config import Config
from kanbanlan.locks import parse_elapsed, process_elapsed_seconds
from kanbanlan.registry import Registration, RegistryStore, describe_problem, utc_now
from kanbanlan.runner import CommandResult, RateLimitError
from kanbanlan.snapshot import SCHEMA_VERSION, CacheStore, isoformat
from kanbanlan.worker import (
    GraphQLPointMeter,
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
            # Each repository has its own Project here, so this test isolates
            # account cooldowns from the shared-Project rotation.
            projects = {"a": 2, "b": 3, "c": 4, "d": 5}
            with (
                mock.patch(
                    "kanbanlan.worker.Config.load",
                    side_effect=lambda path: Config(
                        f"acme/{path.name}", "acme", "organization", projects[path.name]
                    ),
                ),
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


def _git_clone(path: Path, *, active_at: float) -> Path:
    """Create a minimal checkout whose Git activity files carry ``active_at``."""

    common = path / ".git"
    common.mkdir(parents=True)
    for name in ("HEAD", "index"):
        (common / name).write_text("ref: refs/heads/main\n", encoding="utf-8")
        os.utime(common / name, (active_at, active_at))
    return common


class CycleDeduplicationTests(unittest.TestCase):
    def test_duplicate_registrations_of_one_repository_refresh_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            registry = RegistryStore(base / "state")
            now = time.time()
            live = _git_clone(base / "live", active_at=now)
            stale = _git_clone(base / "stale", active_at=now - 90 * 86400)
            for common in (stale, live):
                registry.register(
                    common_dir=common,
                    root=common.parent,
                    repository="Acme/One",
                    hostname="github.com",
                    github_login="alice",
                )
            worker = Worker(registry)
            with mock.patch.object(worker, "_run_registration") as run:
                summary = worker.run_once()

            run.assert_called_once()
            self.assertEqual(str(live.parent.resolve()), run.call_args.args[0].root)
            self.assertEqual(1, summary["succeeded"])
            self.assertEqual(1, summary["skipped"])
            duplicate = next(v for v in summary["repositories"] if v["status"] == "duplicate")
            self.assertEqual(str(stale.parent.resolve()), duplicate["root"])
            self.assertEqual(str(live.parent.resolve()), duplicate["serviced_root"])

            status = worker_status(registry)
            problems = [v for v in status["problems"] if v["kind"] == "duplicate_repository"]
            self.assertEqual(1, len(problems))
            self.assertEqual(str(live.parent.resolve()), problems[0]["serviced_root"])
            skipped = {v["root"]: v["duplicate_skipped"] for v in status["repositories"]}
            self.assertEqual(
                {str(live.parent.resolve()): False, str(stale.parent.resolve()): True}, skipped
            )
            self.assertIn("registered 2 times", describe_problem(problems[0]))

    def test_usable_checkout_wins_over_a_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            registry = RegistryStore(base / "state")
            live = _git_clone(base / "live", active_at=time.time() - 86400)
            registry.register(
                common_dir=live,
                root=live.parent,
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            registry.register(
                common_dir=base / "gone" / ".git",
                root=base / "gone",
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            (base / "plain").mkdir()
            registry.register(
                common_dir=base / "plain" / ".git",
                root=base / "plain",
                repository="acme/two",
                hostname="github.com",
                github_login="alice",
            )
            worker = Worker(registry)
            with mock.patch.object(worker, "_run_registration") as run:
                worker.run_once()

            roots = sorted(call.args[0].root for call in run.call_args_list)
            self.assertEqual(
                sorted([str(live.parent.resolve()), str((base / "plain").resolve())]), roots
            )
            status = worker_status(registry)
            states = {v["root"]: v["root_state"] for v in status["repositories"]}
            self.assertEqual("ok", states[str(live.parent.resolve())])
            self.assertEqual("missing_root", states[str((base / "gone").resolve())])
            self.assertEqual("not_a_git_checkout", states[str((base / "plain").resolve())])
            kinds = sorted(v["kind"] for v in status["problems"])
            self.assertEqual(["duplicate_repository", "missing_root", "not_a_git_checkout"], kinds)
            messages = [describe_problem(v) for v in status["problems"]]
            self.assertTrue(any("no longer exists" in value for value in messages))
            self.assertTrue(any("not a Git checkout" in value for value in messages))

    def test_shared_project_is_refreshed_once_per_cycle_in_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            registry = RegistryStore(base / "state")
            for name in ("a", "b"):
                registry.register(
                    common_dir=base / name / ".git",
                    root=base / name,
                    repository=f"acme/{name}",
                    hostname="github.com",
                    github_login="alice",
                    interval_seconds=60,
                )
            older = registry.get(str(base / "b" / ".git"))
            older.last_run_at = isoformat(datetime.now(UTC) - timedelta(hours=1))
            registry.update(older)
            newer = registry.get(str(base / "a" / ".git"))
            newer.last_run_at = isoformat(datetime.now(UTC) - timedelta(minutes=5))
            registry.update(newer)
            shared = Config("acme/a", "acme", "organization", 7)
            worker = Worker(registry)

            def serviced(registration: Registration) -> None:
                registration.last_run_at = utc_now()
                registry.update(registration)

            with (
                mock.patch("kanbanlan.worker.Config.load", return_value=shared),
                mock.patch.object(worker, "_run_registration", side_effect=serviced) as run,
            ):
                first = worker.run_once()
                self.assertEqual(["acme/b"], [c.args[0].repository for c in run.call_args_list])
                deferred = next(
                    v for v in first["repositories"] if v["status"] == "project_refreshed"
                )
                self.assertEqual(
                    {"repository": "acme/a", "refreshed_by": "acme/b"},
                    {key: deferred[key] for key in ("repository", "refreshed_by")},
                )
                run.reset_mock()
                # b ran moments ago and is not due; a, deferred last cycle, runs now.
                worker.run_once()
                self.assertEqual(["acme/a"], [c.args[0].repository for c in run.call_args_list])

    def test_failed_refresh_does_not_defer_a_repository_sharing_its_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            registry = RegistryStore(base / "state")
            for name in ("a", "b"):
                registry.register(
                    common_dir=base / name / ".git",
                    root=base / name,
                    repository=f"acme/{name}",
                    hostname="github.com",
                    github_login="alice",
                )
            shared = Config("acme/a", "acme", "organization", 7)
            worker = Worker(registry)
            with (
                mock.patch("kanbanlan.worker.Config.load", return_value=shared),
                mock.patch.object(
                    worker, "_run_registration", side_effect=[RuntimeError("boom"), None]
                ) as run,
            ):
                summary = worker.run_once()
            self.assertEqual(2, run.call_count)
            self.assertEqual((1, 1), (summary["failed"], summary["succeeded"]))


class GraphQLPointTests(unittest.TestCase):
    def test_meter_totals_reported_costs_and_counts_mutations(self) -> None:
        inner = mock.Mock()
        responses = {
            "query": json.dumps({"data": {"rateLimit": {"cost": 3, "remaining": 10}}}),
            "mutation": json.dumps({"data": {"updateItem": {}}}),
        }
        inner.run.side_effect = lambda args, **_: CommandResult(
            tuple(args), 0, responses.get(args[-1], "[]"), ""
        )
        meter = GraphQLPointMeter(inner)
        meter.run(["gh", "api", "graphql", "query"], retry=True)
        meter.run(["gh", "api", "graphql", "query"])
        meter.run(["gh", "api", "graphql", "mutation"])
        meter.run(["gh", "issue", "list"])
        self.assertEqual(7, meter.points)
        self.assertIs(inner.env, meter.env)

    def test_iteration_records_points_spent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            store.register(
                common_dir=Path(directory) / "common",
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            inner = mock.Mock()
            inner.run.return_value = CommandResult(
                ("gh",), 0, json.dumps({"data": {"rateLimit": {"cost": 4}}}), ""
            )

            def provider(_root, _config, *, runner):
                runner.run(["gh", "api", "graphql", "--input", "-"])
                runner.run(["gh", "api", "graphql", "--input", "-"])
                value = mock.Mock()
                value.list_open_requests.return_value = []
                return value

            cache = mock.Mock()
            cache.refresh.return_value = {"items": []}
            with (
                mock.patch("kanbanlan.worker.Config.load"),
                mock.patch("kanbanlan.worker.scoped_runner", return_value=inner),
                mock.patch("kanbanlan.worker.GitHub", side_effect=provider),
                mock.patch("kanbanlan.worker.cache_dir", return_value=Path(directory) / "cache"),
                mock.patch("kanbanlan.worker.CacheStore", return_value=cache),
                mock.patch("kanbanlan.worker.drain_outbox"),
                mock.patch("kanbanlan.worker.plan_reconciliation", return_value=[]),
            ):
                Worker(store).run_once()

            self.assertEqual(8, store.registrations()[0].last_graphql_points)
            self.assertEqual(8, worker_status(store)["repositories"][0]["last_graphql_points"])


class SingleInstanceTests(unittest.TestCase):
    def test_lock_owner_refreshes_its_lock_and_notices_a_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "worker.lock"
            with WorkerLock(lock_path) as lock:
                os.utime(lock_path, (1, 1))
                self.assertTrue(lock.still_held())
                self.assertGreater(lock_path.stat().st_mtime, 1)
                lock_path.unlink()
                lock_path.write_text(json.dumps({"pid": os.getpid(), "nonce": "other"}))
                self.assertFalse(lock.still_held())
            # The successor's lock is not ours to remove.
            self.assertTrue(lock_path.exists())

    def test_worker_exits_when_another_live_worker_owns_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            lock_path = Path(directory) / "worker.lock"
            successor = {"pid": os.getppid(), "nonce": "successor"}

            def taken_over(_seconds: float) -> None:
                lock_path.unlink()
                lock_path.write_text(json.dumps(successor), encoding="utf-8")

            result = Worker(store, sleep=taken_over).run_forever()

            self.assertTrue(result["stopped"])
            self.assertEqual(successor, json.loads(lock_path.read_text(encoding="utf-8")))

    def test_worker_reclaims_a_swept_lock_and_keeps_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            lock_path = Path(directory) / "worker.lock"
            sleeps: list[float] = []

            def sweep_then_stop(seconds: float) -> None:
                sleeps.append(seconds)
                if len(sleeps) == 1:
                    lock_path.unlink()
                    return
                self.assertEqual(os.getpid(), json.loads(lock_path.read_text())["pid"])
                raise StopIteration

            with self.assertRaises(StopIteration):
                Worker(store, sleep=sweep_then_stop).run_forever()
            self.assertEqual(2, len(sleeps))
            self.assertFalse(lock_path.exists())

    def test_start_does_not_launch_beside_a_live_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            with (
                WorkerLock(Path(directory) / "worker.lock"),
                mock.patch("kanbanlan.worker.subprocess") as launcher,
            ):
                payload = start_worker(store)
            launcher.Popen.assert_not_called()
            self.assertEqual(os.getpid(), payload["worker"]["pid"])

    def test_long_lived_owner_is_verified_where_ps_lacks_etimes(self) -> None:
        def fake_ps(args, **_kwargs):
            keyword = args[2]
            if keyword == "etimes=":
                return CommandResult(tuple(args), 1, "", "ps: etimes: keyword not found")
            return CommandResult(tuple(args), 0, " 1-02:03:04\n", "")

        with mock.patch("kanbanlan.locks.subprocess.run", side_effect=fake_ps):
            self.assertEqual(93784.0, process_elapsed_seconds(123))

    def test_elapsed_time_parsing(self) -> None:
        self.assertEqual(307.0, parse_elapsed("05:07"))
        self.assertEqual(3723.0, parse_elapsed("1:02:03"))
        self.assertEqual(273906.0, parse_elapsed("3-04:05:06"))
        for value in (None, "", "abc", "1:2:3:4", "x-01:02"):
            self.assertIsNone(parse_elapsed(value))
