from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kanbanlan.locks import FileLock
from kanbanlan.registry import RegistryStore


class RegistryTests(unittest.TestCase):
    def test_registration_is_deduplicated_by_common_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            common = Path(directory) / "common"
            primary = Path(directory) / "one"
            linked = Path(directory) / "worktree"
            primary.mkdir()
            linked.mkdir()
            first = store.register(
                common_dir=common,
                root=primary,
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            second = store.register(
                common_dir=common,
                root=linked,
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )

            self.assertEqual(first.common_dir, second.common_dir)
            self.assertEqual(1, len(store.registrations()))
            self.assertEqual(str(primary.resolve()), second.root)

    def test_older_registry_entries_receive_new_field_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            common = str((Path(directory) / "common").resolve())
            store.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "repositories": {
                            common: {
                                "common_dir": common,
                                "root": directory,
                                "repository": "acme/one",
                                "hostname": "github.com",
                                "github_login": "alice",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            registration = store.registrations()[0]

            self.assertTrue(registration.enabled)
            self.assertFalse(registration.disabled)
            self.assertEqual(300, registration.interval_seconds)

    def test_corrupt_registry_is_reported_instead_of_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            store.path.write_text("not json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "could not read worker registry"):
                store.register(
                    common_dir=Path(directory) / "common",
                    root=Path(directory),
                    repository="acme/one",
                    hostname="github.com",
                    github_login="alice",
                )

            self.assertEqual("not json", store.path.read_text(encoding="utf-8"))

    def test_disable_is_an_explicit_persistent_tombstone(self) -> None:
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
            disabled = store.disable(common)
            self.assertFalse(disabled.enabled)
            self.assertTrue(disabled.disabled)
            re_registered = store.register(
                common_dir=common,
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            self.assertFalse(re_registered.enabled)
            self.assertTrue(re_registered.disabled)
            self.assertEqual("0600", oct((Path(directory) / "registry.json").stat().st_mode)[-4:])

    def test_health_fields_round_trip_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            common = Path(directory) / "common"
            registration = store.register(
                common_dir=common,
                root=Path(directory),
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            registration.consecutive_failures = 2
            registration.last_error = {"kind": "TimeoutError", "message": "temporary"}
            store.update(registration)
            payload = json.loads((Path(directory) / "registry.json").read_text())
            self.assertEqual(
                2, payload["repositories"][str(common.resolve())]["consecutive_failures"]
            )
            self.assertEqual("0600", oct((Path(directory) / "registry.json").stat().st_mode)[-4:])
            self.assertEqual("0700", oct(Path(directory).stat().st_mode)[-4:])

    def test_registry_lock_never_steals_from_a_live_owner(self) -> None:
        """The registry uses the owner-verified lock: age alone frees nothing."""

        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            store.lock_path.write_text(json.dumps({"pid": os.getpid()}) + "\n", encoding="utf-8")
            old = time.time() - 3600
            os.utime(store.lock_path, (old, old))
            with (
                mock.patch("kanbanlan.locks.process_elapsed_seconds", return_value=7200.0),
                mock.patch(
                    "kanbanlan.registry.FileLock",
                    lambda path: FileLock(path, timeout=0.2),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    store.register(
                        common_dir=Path(directory) / "common",
                        root=Path(directory),
                        repository="acme/one",
                        hostname="github.com",
                        github_login="alice",
                    )
            owner = json.loads(store.lock_path.read_text(encoding="utf-8"))
            self.assertEqual(os.getpid(), owner["pid"])

    def test_registry_lock_replaces_a_provably_dead_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            store.lock_path.write_text(
                '{"pid": 999999, "started_at": "earlier"}\n', encoding="utf-8"
            )
            with mock.patch("kanbanlan.locks.pid_running", return_value=False):
                registration = store.register(
                    common_dir=Path(directory) / "common",
                    root=Path(directory),
                    repository="acme/one",
                    hostname="github.com",
                    github_login="alice",
                )
            self.assertEqual("acme/one", registration.repository)
            self.assertFalse(store.lock_path.exists())
