from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest import mock

from cli_patch import patch_cli

from kanbanlan.cli.parser import build_parser
from kanbanlan.cli.records import _cmd_record
from kanbanlan.config import Config
from kanbanlan.identity import attach_kanbanlan_id
from kanbanlan.records import (
    create_record,
    current_branch,
    pull_request_sections,
    record_path,
    render_record,
    render_record_from_pull_request,
    select_pull_request,
    write_record_from_pull_request,
)
from kanbanlan.scaffold import pull_request_template
from kanbanlan.snapshot import build_snapshot

KANBANLAN_ID = "KBL-ABCDEFGHIJKLMNOPQRSTUVWXYZ"

COMPLETE_BODY = """<!-- Template guidance. -->

## Request

Kanbanlan: KBL-ABCDEFGHIJKLMNOPQRSTUVWXYZ

Closes #7

## Decisions

<!-- Durable implementation decisions. -->

- Parse the body instead of asking twice.

### Detail

Level-three headings stay inside the section.

## Verification

```sh
## not a heading inside a fence
uv run pytest
```

## Result

Records come from the pull request.

## Security and compatibility

None.
"""


def config() -> Config:
    return Config(
        repository="acme/widget",
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def item() -> dict:
    raw = {
        "id": "item-7",
        "type": "ISSUE",
        "isArchived": False,
        "fieldValues": {"nodes": [{"name": "Ready", "field": {"name": "Status"}}]},
        "content": {
            "id": "issue-7",
            "number": 7,
            "title": "Portable request",
            "body": attach_kanbanlan_id("## Outcome\n\nShip it.", KANBANLAN_ID),
            "url": "https://github.test/acme/widget/issues/7",
            "state": "OPEN",
            "stateReason": None,
            "createdAt": "2026-07-25T00:00:00Z",
            "updatedAt": "2026-07-25T01:00:00Z",
            "closedAt": None,
            "repository": {"nameWithOwner": "acme/widget"},
            "labels": {"nodes": []},
            "assignees": {"nodes": []},
            "comments": {"nodes": []},
        },
    }
    project = {
        "id": "project",
        "number": 2,
        "title": "Delivery",
        "url": "https://github.test/projects/2",
        "fields": {"nodes": []},
        "items": [raw],
    }
    snapshot = build_snapshot(config(), project, [], {}, datetime(2026, 7, 25, tzinfo=UTC))
    return snapshot["items"][0]


def pull_request(body: str = COMPLETE_BODY, *, number: int = 12, head: str = "work/x") -> dict:
    return {
        "number": number,
        "url": f"https://github.test/acme/widget/pull/{number}",
        "body": body,
        "head_ref": head,
    }


class PullRequestSectionTests(unittest.TestCase):
    def test_sections_split_on_level_two_headings_and_drop_comments(self) -> None:
        sections = pull_request_sections(COMPLETE_BODY)

        self.assertEqual(
            ["request", "decisions", "verification", "result", "security and compatibility"],
            list(sections),
        )
        self.assertTrue(sections["decisions"].startswith("- Parse the body"))
        self.assertIn("### Detail", sections["decisions"])
        self.assertNotIn("<!--", sections["decisions"])
        self.assertIn("## not a heading inside a fence", sections["verification"])
        self.assertEqual("Records come from the pull request.", sections["result"])

    def test_empty_body_has_no_sections(self) -> None:
        self.assertEqual({}, pull_request_sections(None))
        self.assertEqual({}, pull_request_sections(""))

    def test_level_one_heading_ends_a_section(self) -> None:
        sections = pull_request_sections("## Result\n\nDone.\n\n# Appendix\n\nIgnored.\n")

        self.assertEqual({"result": "Done."}, sections)


class RenderFromPullRequestTests(unittest.TestCase):
    def test_complete_body_renders_every_record_section(self) -> None:
        rendered = render_record_from_pull_request(item(), pull_request())

        self.assertEqual((), rendered.missing)
        self.assertIn("- Kanbanlan: `KBL-ABCDEFGHIJKLMNOPQRSTUVWXYZ`", rendered.text)
        self.assertIn(
            "- Pull request: [#12](https://github.test/acme/widget/pull/12)", rendered.text
        )
        self.assertIn("## Request\n\n## Outcome\n\nShip it.", rendered.text)
        self.assertIn("## Decisions\n\n- Parse the body", rendered.text)
        self.assertIn("## Delivered result\n\nRecords come from the pull request.", rendered.text)
        self.assertNotIn("Security and compatibility", rendered.text)

    def test_record_headings_match_the_existing_format(self) -> None:
        generated = render_record_from_pull_request(item(), pull_request()).text
        original = render_record(item())

        self.assertEqual(
            list(pull_request_sections(original)), list(pull_request_sections(generated))
        )
        self.assertEqual(original.split("\n\n## ")[0], generated.split("\n- Pull request:")[0])

    def test_missing_and_placeholder_sections_are_reported_not_invented(self) -> None:
        body = "## Decisions\n\n<!-- Fill me in. -->\n\n## Verification\n\nRan tests.\n"
        rendered = render_record_from_pull_request(item(), pull_request(body))

        self.assertEqual(("Decisions", "Delivered result"), rendered.missing)
        self.assertIn("## Verification\n\nRan tests.", rendered.text)
        self.assertIn("<!-- Missing from #12: add a 'Decisions' section. -->", rendered.text)
        self.assertIn("<!-- Missing from #12: add a 'Result' section. -->", rendered.text)

    def test_delivered_result_heading_is_accepted(self) -> None:
        body = "## Decisions\n\nA.\n\n## Verification\n\nB.\n\n## Delivered result\n\nC.\n"

        rendered = render_record_from_pull_request(item(), pull_request(body))

        self.assertEqual((), rendered.missing)
        self.assertIn("## Delivered result\n\nC.", rendered.text)

    def test_scaffolded_template_is_entirely_missing_until_filled(self) -> None:
        body = pull_request_template(config())

        rendered = render_record_from_pull_request(item(), pull_request(body))

        self.assertEqual(("Decisions", "Verification", "Delivered result"), rendered.missing)
        self.assertEqual(
            {"request", "decisions", "verification", "result"},
            {"request", "decisions", "verification", "result"} & set(pull_request_sections(body)),
        )


class WriteFromPullRequestTests(unittest.TestCase):
    def test_writes_then_updates_then_leaves_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created, _ = write_record_from_pull_request(root, item(), pull_request())
            repeated, _ = write_record_from_pull_request(root, item(), pull_request())
            edited = COMPLETE_BODY.replace("Records come", "Records now come")
            updated, missing = write_record_from_pull_request(root, item(), pull_request(edited))

            self.assertEqual("created", created.action)
            self.assertEqual("unchanged", repeated.action)
            self.assertEqual("updated", updated.action)
            self.assertEqual((), missing)
            self.assertEqual(record_path(root, KANBANLAN_ID), updated.path)
            self.assertIn("Records now come", updated.path.read_text(encoding="utf-8"))

    def test_replaces_a_record_created_by_plain_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_record(root, item())
            result, _ = write_record_from_pull_request(root, item(), pull_request())

            self.assertEqual("updated", result.action)
            self.assertIn("Parse the body", result.path.read_text(encoding="utf-8"))


class SelectPullRequestTests(unittest.TestCase):
    def test_current_branch_wins_among_several(self) -> None:
        card = {
            **item(),
            "linked_open_pull_requests": [
                pull_request(number=1, head="work/a"),
                pull_request(number=2, head="work/b"),
            ],
        }

        self.assertEqual(2, select_pull_request(card, "work/b")["number"])

    def test_single_linked_pull_request_is_used_off_branch(self) -> None:
        card = {**item(), "linked_open_pull_requests": [pull_request(number=3)]}

        self.assertEqual(3, select_pull_request(card, "main")["number"])
        self.assertEqual(3, select_pull_request(card, None)["number"])

    def test_no_or_ambiguous_pull_requests_are_errors(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no open pull request"):
            select_pull_request({**item(), "linked_open_pull_requests": []}, "work/a")
        ambiguous = {
            **item(),
            "linked_open_pull_requests": [
                pull_request(number=1, head="work/a"),
                pull_request(number=2, head="work/b"),
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "several open pull requests"):
            select_pull_request(ambiguous, "main")


class CurrentBranchTests(unittest.TestCase):
    def test_reads_the_checked_out_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["git", "init", "-q", "-b", "work/topic", str(root)],
                check=True,
                capture_output=True,
            )

            self.assertEqual("work/topic", current_branch(root))

    def test_outside_a_repository_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(current_branch(Path(directory)))


class RecordCommandTests(unittest.TestCase):
    def _run(self, root: Path, body: str, *, json_output: bool = False) -> tuple[int, str, str]:
        card = {**item(), "linked_open_pull_requests": [pull_request(body)]}
        argv = ["--json"] if json_output else []
        args = build_parser().parse_args([*argv, "record", "#7", "--from-pr"])
        stdout, stderr = StringIO(), StringIO()
        with (
            patch_cli("_root", return_value=root),
            mock.patch("kanbanlan.config.Config.load", return_value=config()),
            patch_cli("create_provider"),
            patch_cli("cache_dir", return_value=root / "cache"),
            patch_cli("_read_card", return_value={"items": [card]}) as read,
            patch_cli("_issue", return_value=card),
            patch_cli("current_branch", return_value="work/x"),
            patch_cli("_local_view") as local_view,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = _cmd_record(args)
        self.assertTrue(read.call_args.kwargs["pull_requests"])
        local_view.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_from_pr_writes_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, stdout, stderr = self._run(root, COMPLETE_BODY)

            self.assertEqual(0, code)
            self.assertIn("created: docs/kanbanlan/requests/", stdout)
            self.assertNotIn("missing", stderr)
            self.assertTrue(record_path(root, KANBANLAN_ID).exists())

    def test_from_pr_reports_missing_sections_and_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, _, stderr = self._run(root, "## Decisions\n\nA.\n")

            self.assertEqual(1, code)
            self.assertIn("missing from the pull request: Verification, Delivered result", stderr)

    def test_from_pr_json_lists_missing_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, stdout, _ = self._run(Path(directory), "## Result\n\nC.\n", json_output=True)

            self.assertEqual(1, code)
            self.assertIn('"missing_sections": [', stdout)
            self.assertIn('"Decisions"', stdout)

    def test_default_record_behaviour_is_unchanged(self) -> None:
        args = build_parser().parse_args(["record", "#7"])

        self.assertFalse(args.from_pr)


if __name__ == "__main__":
    unittest.main()
