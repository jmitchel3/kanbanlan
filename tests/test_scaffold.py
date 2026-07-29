from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kanbanlan.config import Config
from kanbanlan.scaffold import (
    END_MARKER,
    MANAGED_FILE_MARKER,
    START_MARKER,
    YAML_MANAGED_FILE_MARKER,
    scaffold_repository,
)


def config() -> Config:
    return Config(
        repository="acme/widget",
        project_owner="acme",
        project_owner_type="organization",
        project_number=7,
    )


class ScaffoldTests(unittest.TestCase):
    def test_session_hooks_are_created_only_after_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_repository(root, config())
            self.assertFalse((root / ".codex" / "hooks.json").exists())

            tracked = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
                session_tracking=True,
            )
            scaffold_repository(root, tracked)

            codex = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
            claude = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
            grok = json.loads(
                (root / ".grok" / "hooks" / "kanbanlan.json").read_text(encoding="utf-8")
            )
            agy = json.loads((root / ".agents" / "hooks.json").read_text(encoding="utf-8"))

        self.assertIn("session-hook --agent codex", str(codex))
        self.assertIn("session-hook --agent claude", str(claude))
        self.assertIn("session-hook --agent grok", str(grok))
        self.assertIn("session-hook --agent agy", str(agy))

    def test_session_hook_scaffolding_preserves_existing_custom_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".codex" / "hooks.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"custom": true}\n', encoding="utf-8")
            tracked = Config(
                repository="acme/widget",
                project_owner="acme",
                project_owner_type="organization",
                project_number=7,
                session_tracking=True,
            )

            results = scaffold_repository(root, tracked)
            result = next(value for value in results if value.path == path)

        self.assertEqual("skipped (custom file)", result.action)

    def test_gitignore_adds_both_local_state_entries_on_first_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_results = scaffold_repository(root, config())
            first = (root / ".gitignore").read_text(encoding="utf-8")
            second_results = scaffold_repository(root, config())
            second = (root / ".gitignore").read_text(encoding="utf-8")

        self.assertEqual(
            "created",
            next(value.action for value in first_results if value.path.name == ".gitignore"),
        )
        self.assertEqual(
            "unchanged",
            next(value.action for value in second_results if value.path.name == ".gitignore"),
        )
        self.assertEqual(first, second)
        self.assertEqual(1, first.splitlines().count("/.cache/kanbanlan/"))
        self.assertEqual(1, first.splitlines().count("/.worktrees/"))

    def test_gitignore_migrates_cache_only_entry_and_preserves_custom_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".gitignore"
            path.write_text(
                "custom-output/\n\n# Local Kanbanlan coordination cache\n/.cache/kanbanlan/\n",
                encoding="utf-8",
            )

            scaffold_repository(root, config())
            first = path.read_text(encoding="utf-8")
            scaffold_repository(root, config())
            second = path.read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertIn("custom-output/", first)
        self.assertIn("# Local Kanbanlan coordination state", first)
        self.assertNotIn("# Local Kanbanlan coordination cache", first)
        self.assertEqual(1, first.splitlines().count("/.cache/kanbanlan/"))
        self.assertEqual(1, first.splitlines().count("/.worktrees/"))

    def test_scaffold_is_idempotent_and_preserves_unmanaged_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("# Rules\n\nKeep me.\n", encoding="utf-8")
            scaffold_repository(root, config())
            first = (root / "AGENTS.md").read_text(encoding="utf-8")
            second_results = scaffold_repository(root, config())
            second = (root / "AGENTS.md").read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertIn("Keep me.", second)
        self.assertEqual(1, second.count(START_MARKER))
        self.assertEqual(1, second.count(END_MARKER))
        self.assertEqual({"unchanged"}, {result.action for result in second_results})

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

    def test_issue_form_uses_a_yaml_comment_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_repository(root, config())
            issue_form = (root / ".github" / "ISSUE_TEMPLATE" / "work-request.yml").read_text(
                encoding="utf-8"
            )

        self.assertTrue(issue_form.startswith(f"{YAML_MANAGED_FILE_MARKER}\n"))
        self.assertNotIn(MANAGED_FILE_MARKER, issue_form)

    def test_issue_form_migrates_the_legacy_html_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".github" / "ISSUE_TEMPLATE" / "work-request.yml"
            path.parent.mkdir(parents=True)
            path.write_text(f"{MANAGED_FILE_MARKER}\nname: Old\n", encoding="utf-8")

            scaffold_repository(root, config())

            issue_form = path.read_text(encoding="utf-8")

        self.assertTrue(issue_form.startswith(f"{YAML_MANAGED_FILE_MARKER}\n"))
        self.assertIn("name: Work request", issue_form)
