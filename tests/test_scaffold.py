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


HEADING = "## Request Coordination Workflow"


class MalformedMarkerTests(unittest.TestCase):
    def _run(self, text: str) -> tuple[str, str, str | None]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "CLAUDE.md"
            path.write_bytes(text.encode("utf-8"))
            results = scaffold_repository(root, config())
            result = next(value for value in results if value.path == path)
            return path.read_bytes().decode("utf-8"), result.action, result.detail

    def test_lone_end_marker_after_generated_heading_is_replaced(self) -> None:
        # The positiveform.com shape: the old block began at the heading and
        # only the end marker was ever written.
        before = "# CLAUDE.md\r\n\r\nProject rules.\n\n## Testing\n\nRun `make test`.\n\n"
        stale = f"{HEADING}\n\nRequests live on an old board.\n\n- Run `kanbanlan ensure`.\n"
        after = "\n\n## Deploys\n\nNever deploy from local.  \n"
        rendered, action, detail = self._run(f"{before}{stale}{END_MARKER}{after}")

        self.assertEqual("updated", action)
        self.assertIsNone(detail)
        self.assertTrue(rendered.startswith(before))
        self.assertTrue(rendered.endswith(f"{END_MARKER}{after}"))
        self.assertEqual(1, rendered.count(START_MARKER))
        self.assertEqual(1, rendered.count(END_MARKER))
        self.assertEqual(1, rendered.count(HEADING))
        self.assertNotIn("old board", rendered)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CLAUDE.md").write_text(f"{before}{stale}{END_MARKER}{after}", "utf-8")
            scaffold_repository(root, config())
            first = (root / "CLAUDE.md").read_bytes()
            again = scaffold_repository(root, config())
            self.assertEqual(first, (root / "CLAUDE.md").read_bytes())
        self.assertEqual("unchanged", next(r.action for r in again if r.path.name == "CLAUDE.md"))

    def test_lone_end_marker_uses_the_last_heading_above_it(self) -> None:
        before = f"# CLAUDE.md\n\n{HEADING}\n\nOlder hand-written notes.\n\n"
        text = f"{before}{HEADING}\n\nstale\n{END_MARKER}\n"
        rendered, action, _ = self._run(text)

        self.assertEqual("updated", action)
        self.assertTrue(rendered.startswith(f"{before}{START_MARKER}\n{HEADING}"))
        self.assertNotIn("stale", rendered)

    def assert_left_untouched(self, text: str, fragment: str) -> None:
        rendered, action, detail = self._run(text)
        self.assertEqual(text, rendered)
        self.assertEqual("needs repair", action)
        assert detail is not None
        self.assertIn(fragment, detail)
        self.assertIn("by hand", detail)

    def test_lone_end_marker_without_the_heading_needs_repair(self) -> None:
        self.assert_left_untouched(
            f"# CLAUDE.md\n\n## Other\n\nnotes\n{END_MARKER}\n", "no matching"
        )

    def test_lone_end_marker_with_heading_only_below_needs_repair(self) -> None:
        self.assert_left_untouched(
            f"# CLAUDE.md\n\n{END_MARKER}\n\n{HEADING}\n\nnotes\n", "no matching"
        )

    def test_lone_start_marker_needs_repair(self) -> None:
        self.assert_left_untouched(
            f"# CLAUDE.md\n\n{START_MARKER}\n{HEADING}\n\nnotes\n", "no matching"
        )

    def test_end_before_start_needs_repair(self) -> None:
        self.assert_left_untouched(
            f"# CLAUDE.md\n\n{END_MARKER}\n{HEADING}\n{START_MARKER}\n", "appears before"
        )

    def test_multiple_blocks_need_repair(self) -> None:
        block = f"{START_MARKER}\n{HEADING}\n\nnotes\n{END_MARKER}\n"
        self.assert_left_untouched(f"# CLAUDE.md\n\n{block}\n{block}", "found 2")

    def test_extra_end_marker_needs_repair(self) -> None:
        block = f"{START_MARKER}\n{HEADING}\n\nnotes\n{END_MARKER}\n"
        self.assert_left_untouched(f"# CLAUDE.md\n\n{block}{END_MARKER}\n", "found 1")

    def test_well_formed_block_preserves_surrounding_bytes(self) -> None:
        before = "# CLAUDE.md\r\n\r\nKeep\tthis.  \n\n"
        after = "\n\n## Tail\n\nno trailing newline"
        rendered, action, _ = self._run(f"{before}{START_MARKER}\nold\n{END_MARKER}{after}")

        self.assertEqual("updated", action)
        self.assertTrue(rendered.startswith(f"{before}{START_MARKER}\n{HEADING}"))
        self.assertTrue(rendered.endswith(f"{END_MARKER}{after}"))

    def test_init_output_warns_about_files_that_need_repair(self) -> None:
        import contextlib
        import io

        from kanbanlan.cli.setup import _print_scaffold_results

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CLAUDE.md").write_text(f"{START_MARKER}\nnotes\n", encoding="utf-8")
            results = scaffold_repository(root, config())
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                _print_scaffold_results(results, root)

        combined = out.getvalue() + err.getvalue()
        self.assertIn("needs repair", combined)
        self.assertIn("CLAUDE.md was left unchanged", combined)
