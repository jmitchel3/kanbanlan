from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from test_project_scope import (
    LOCAL,
    PEER,
    FakeGitHub,
    issue_item,
    project_page,
    pull_request,
    pull_request_page,
)
from test_snapshot import _CountingClient, config

from kanbanlan.cli import _refresh_after_mutation, _set_state
from kanbanlan.snapshot import SCHEMA_VERSION, CacheStore, isoformat
from kanbanlan.workflow import read_board


def _snapshot_at(moment: datetime) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "generated_at": isoformat(moment), "rate_limit": {}}


class RefreshCoalescingTests(unittest.TestCase):
    def test_refresh_reuses_a_fetch_that_started_after_it_was_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            client = _CountingClient()

            class FetchedWhileWaiting:
                def __init__(self, path, timeout: float = 10.0) -> None:
                    pass

                def __enter__(self):
                    # Another session held the lock and finished a fetch it
                    # began after this refresh was requested.
                    store._write_json(store.snapshot_path, _snapshot_at(datetime.now(UTC)))
                    return self

                def __exit__(self, *args) -> None:
                    pass

            with mock.patch("kanbanlan.snapshot.FileLock", FetchedWhileWaiting):
                store.refresh(client)

            self.assertEqual(0, client.fetches)

    def test_refresh_never_reuses_a_fetch_that_began_before_it_was_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            # Fresh by the staleness window, but older than this call, so it
            # may predate a write the caller just made.
            store._write_json(
                store.snapshot_path, _snapshot_at(datetime.now(UTC) - timedelta(seconds=1))
            )
            client = _CountingClient()

            store.refresh(client)

            self.assertEqual(1, client.fetches)

    def test_a_refresh_requested_mid_fetch_fetches_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            started = threading.Event()
            release = threading.Event()

            class SlowClient(_CountingClient):
                def fetch(self):
                    started.set()
                    release.wait(5)
                    return super().fetch()

            client = SlowClient()
            first = threading.Thread(target=store.refresh, args=(client,))
            first.start()
            self.assertTrue(started.wait(5))
            # Requested while the first fetch is in flight: that fetch began
            # before this request, so the waiter must fetch again itself.
            second = threading.Thread(target=store.refresh, args=(client,))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(5)
            second.join(5)

            self.assertEqual(2, client.fetches)


class InvalidationTests(unittest.TestCase):
    def test_invalidation_makes_a_fresh_snapshot_stale_until_the_next_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            client = _CountingClient()
            store.refresh(client)
            self.assertTrue(store.is_fresh())

            time.sleep(0.01)
            store.invalidate()

            self.assertFalse(store.is_fresh())
            self.assertEqual("stale", store.inspect()["snapshot_state"])
            store.ensure(client)
            self.assertEqual(2, client.fetches)
            self.assertTrue(store.is_fresh())

    def test_refresh_does_not_reuse_an_invalidated_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            store._write_json(
                store.snapshot_path, _snapshot_at(datetime.now(UTC) + timedelta(seconds=5))
            )
            store._write_json(
                store.invalidated_path,
                {"invalidated_at": isoformat(datetime.now(UTC) + timedelta(seconds=10))},
            )
            client = _CountingClient()

            store.refresh(client)

            self.assertEqual(1, client.fetches)


class BackgroundRefreshTests(unittest.TestCase):
    def test_mutation_invalidates_and_refreshes_in_a_detached_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            client = _CountingClient()
            store.refresh(client)
            root = Path(directory)

            with (
                mock.patch.dict(os.environ, {"KANBANLAN_BACKGROUND_REFRESH": "1"}),
                mock.patch("kanbanlan.cli.subprocess.Popen") as popen,
            ):
                _refresh_after_mutation(root, store, client)

            self.assertEqual(1, client.fetches)
            self.assertFalse(store.is_fresh())
            command = popen.call_args.args[0]
            self.assertEqual(["-m", "kanbanlan", "-C", str(root), "--json", "refresh"], command[1:])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_foreground_refresh_is_kept_when_background_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            client = _CountingClient()

            with mock.patch("kanbanlan.cli.subprocess.Popen") as popen:
                _refresh_after_mutation(Path(directory), store, client)

            popen.assert_not_called()
            self.assertEqual(1, client.fetches)
            self.assertTrue(store.is_fresh())


class ConcurrentCollectionTests(unittest.TestCase):
    def _github(self, cached: list[str], pages: dict[str, list[dict[str, Any]]]) -> FakeGitHub:
        github = FakeGitHub(
            project_pages=[project_page([issue_item(7), issue_item(8, repository=PEER)])],
            pull_request_pages=pages,
        )
        github._cached_repositories = lambda: cached  # type: ignore[method-assign]
        return github

    def test_a_repository_no_longer_on_the_board_is_read_but_discarded(self) -> None:
        gone = "acme/retired"
        github = self._github(
            [LOCAL, PEER, gone],
            {
                LOCAL: [pull_request_page([pull_request(11)])],
                PEER: [pull_request_page([pull_request(12, repository=PEER)])],
                gone: [pull_request_page([pull_request(13, repository=gone)])],
            },
        )

        read = github.collect()

        self.assertEqual([11, 12], sorted(value["number"] for value in read.pull_requests))
        self.assertEqual([], read.unavailable_repositories)

    def test_a_failing_speculative_read_is_ignored_when_the_repository_is_gone(self) -> None:
        github = self._github(
            [LOCAL, PEER, "acme/retired"],
            {
                LOCAL: [pull_request_page([pull_request(11)])],
                PEER: [pull_request_page([pull_request(12, repository=PEER)])],
            },
        )
        github.unreadable = {"acme/retired": "not found"}

        read = github.collect()

        self.assertEqual([], read.unavailable_repositories)

    def test_a_repository_new_to_the_board_is_read_after_the_project(self) -> None:
        github = self._github(
            [LOCAL],
            {
                LOCAL: [pull_request_page([pull_request(11)])],
                PEER: [pull_request_page([pull_request(12, repository=PEER)])],
            },
        )

        read = github.collect()

        self.assertEqual([11, 12], sorted(value["number"] for value in read.pull_requests))

    def test_pull_request_reads_overlap_the_project_read(self) -> None:
        overlap = threading.Barrier(2, timeout=5)

        class Overlapping(FakeGitHub):
            def graphql(self, query, variables, *, retry=False):
                if "projectV2" in query or variables.get("repo") == "widget":
                    # Deadlocks, and times out, unless both run at once.
                    overlap.wait()
                return super().graphql(query, variables, retry=retry)

        github = Overlapping(
            project_pages=[project_page([issue_item(7)])],
            pull_request_pages={LOCAL: [pull_request_page([pull_request(11)])]},
        )

        read = github.collect()

        self.assertEqual([11], [value["number"] for value in read.pull_requests])


class StateWriteTests(unittest.TestCase):
    def test_label_and_project_status_are_written_concurrently(self) -> None:
        overlap = threading.Barrier(2, timeout=5)
        writes: list[str] = []

        class Provider:
            def set_request_status(self, number, label):
                overlap.wait()
                writes.append(f"label:{number}:{label}")

            def set_projection_status(self, item_id, projection, status):
                overlap.wait()
                writes.append(f"status:{item_id}:{status}")

        store = mock.Mock()
        store.snapshot.return_value = {"project": {"id": "project-1", "fields": {"nodes": []}}}
        item = {"number": 7, "project_item_id": "item-7", "repository": LOCAL}
        _set_state(Provider(), store, item, "status:ready", "Ready")

        self.assertEqual(["label:7:status:ready", "status:item-7:Ready"], sorted(writes))

    def test_a_failed_write_still_raises(self) -> None:
        class Provider:
            def set_request_status(self, number, label):
                raise RuntimeError("label write failed")

            def set_projection_status(self, item_id, projection, status):
                pass

        store = mock.Mock()
        store.snapshot.return_value = {"project": {"id": "project-1", "fields": {"nodes": []}}}
        item = {"number": 7, "project_item_id": "item-7"}
        with self.assertRaisesRegex(RuntimeError, "label write failed"):
            _set_state(Provider(), store, item, "status:ready", "Ready")


class BoardReadTests(unittest.TestCase):
    def test_open_requests_are_listed_while_the_snapshot_refreshes(self) -> None:
        overlap = threading.Barrier(2, timeout=5)

        class Provider(_CountingClient):
            def fetch(self):
                overlap.wait()
                return super().fetch()

            def list_open_requests(self):
                overlap.wait()
                return [{"number": 7}]

        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))

            snapshot, open_requests = read_board(store, Provider())

        self.assertEqual([{"number": 7}], open_requests)
        self.assertEqual("Delivery", snapshot["project"]["title"])
