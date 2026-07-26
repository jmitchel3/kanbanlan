from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kanbanlan.config import Config
from kanbanlan.scaffold import END_MARKER, START_MARKER, scaffold_repository


def config() -> Config:
    return Config(
        repository="acme/widget",
        project_owner="acme",
        project_owner_type="organization",
        project_number=7,
    )


class ScaffoldTests(unittest.TestCase):
    def test_scaffold_is_idempotent_and_preserves_unmanaged_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("# Rules\n\nKeep me.\n", encoding="utf-8")
            scaffold_repository(root, config())
            first = (root / "AGENTS.md").read_text(encoding="utf-8")
            scaffold_repository(root, config())
            second = (root / "AGENTS.md").read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertIn("Keep me.", second)
        self.assertEqual(1, second.count(START_MARKER))
        self.assertEqual(1, second.count(END_MARKER))

    def test_custom_pull_request_template_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".github" / "pull_request_template.md"
            path.parent.mkdir(parents=True)
            path.write_text("custom\n", encoding="utf-8")
            results = scaffold_repository(root, config())
            result = next(value for value in results if value.path == path)

            self.assertEqual("custom\n", path.read_text(encoding="utf-8"))
            self.assertEqual("skipped (custom file)", result.action)
