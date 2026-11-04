from __future__ import annotations

import unittest
from pathlib import Path

from kanbanlan.config import Config
from kanbanlan.scaffold import (
    END_MARKER,
    MANAGED_FILE_MARKER,
    START_MARKER,
    agent_instructions,
    workflow_doc,
)

ROOT = Path(__file__).resolve().parents[1]
REGENERATE = (
    'regenerate with: uv run python -c "from pathlib import Path; '
    "from kanbanlan.config import Config; from kanbanlan.scaffold import scaffold_repository; "
    "scaffold_repository(Path('.'), Config.load(Path('.')))\" "
    "and revert any unrelated .kanbanlan.toml change"
)


def managed_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    _, remainder = text.split(START_MARKER, 1)
    block, _ = remainder.split(END_MARKER, 1)
    return block.strip()


class CheckedInManagedFilesTests(unittest.TestCase):
    """This repository's own generated files must match the current templates."""

    def setUp(self) -> None:
        self.config = Config.load(ROOT)

    def test_agent_instruction_blocks_match_the_template(self) -> None:
        expected = agent_instructions(self.config).strip()
        for name in ("AGENTS.md", "CLAUDE.md"):
            with self.subTest(file=name):
                self.assertEqual(expected, managed_block(ROOT / name), REGENERATE)

    def test_workflow_doc_matches_the_template(self) -> None:
        expected = f"{MANAGED_FILE_MARKER}\n{workflow_doc(self.config).lstrip()}"
        actual = (ROOT / "docs" / "workflow" / "kanbanlan.md").read_text(encoding="utf-8")
        self.assertEqual(expected, actual, REGENERATE)


class AgentInstructionContentTests(unittest.TestCase):
    def config(self, *, session_tracking: bool = False) -> Config:
        return Config(
            repository="acme/widget",
            project_owner="acme",
            project_owner_type="organization",
            project_number=7,
            session_tracking=session_tracking,
        )

    def test_instructions_name_the_four_verbs_and_capture(self) -> None:
        text = agent_instructions(self.config())
        for command in (
            "kanbanlan start",
            "--touchpoints",
            "kanbanlan done",
            "kanbanlan drop",
            "kanbanlan admin capture",
        ):
            self.assertIn(command, text)
        self.assertIn("https://github.com/orgs/acme/projects/7", text)

    def test_instructions_drop_the_session_start_ritual(self) -> None:
        text = agent_instructions(self.config(session_tracking=True))
        for ritual in ("kanbanlan ensure", "kanbanlan reconcile", "kanbanlan overlap"):
            self.assertNotIn(ritual, text)

    def test_instructions_stay_short(self) -> None:
        for tracking in (False, True):
            with self.subTest(session_tracking=tracking):
                lines = agent_instructions(self.config(session_tracking=tracking)).splitlines()
                self.assertLessEqual(len([line for line in lines if line.strip()]), 14)

    def test_workflow_doc_leads_with_the_four_verbs(self) -> None:
        text = workflow_doc(self.config())
        first_section = text.split("\n## ", 2)[1]
        self.assertTrue(first_section.startswith("The four verbs"))
        for verb in ("start", "done", "drop", "status"):
            self.assertIn(f"kanbanlan {verb}", first_section)
        self.assertNotIn("kanbanlan ensure", text)
        self.assertNotIn("worker", text)


if __name__ == "__main__":
    unittest.main()
