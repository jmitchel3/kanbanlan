from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kanbanlan.registry import RegistryStore


class RegistryTests(unittest.TestCase):
    def test_registration_is_deduplicated_by_common_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RegistryStore(Path(directory))
            common = Path(directory) / "common"
            first = store.register(
                common_dir=common,
                root=Path(directory) / "one",
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )
            second = store.register(
                common_dir=common,
                root=Path(directory) / "worktree",
                repository="acme/one",
                hostname="github.com",
                github_login="alice",
            )

            self.assertEqual(first.common_dir, second.common_dir)
            self.assertEqual(1, len(store.registrations()))
            self.assertEqual(str((Path(directory) / "worktree").resolve()), second.root)

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
