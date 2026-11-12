"""Cards a read leaves out still reach the snapshot, and start reads claims live."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from board_fixtures import ALPHA, BETA, item, snapshot
from cli_patch import patch_cli
from test_rest_queue import config, rest_issue
from test_verbs import VerbTestCase

from kanbanlan.cli.parser import build_parser
from kanbanlan.cli.records import _cmd_record
from kanbanlan.github import GitHub, issues_path
from kanbanlan.rest import ConditionalReader
from kanbanlan.runner import CommandResult

REPOSITORY = "acme/widget"
IDENTITY = "KBL-AAAAAAAAAAAAAAAAAAAAAAAAAA"
GITHUB_REAL_REPOSITORY_ISSUES = GitHub._repository_issues


class GitHubPages:
    """Answer ``gh api --include`` exactly as GitHub was measured to.

    The ETag covers the body only, and a ``304`` carries no Link header
    (both checked against api.github.com on 2026-11-12).
    """

    def __init__(self, pages: dict[str, tuple[list, str | None]]):
        self.pages = pages
        self.calls: list[list[str]] = []

    def run(self, args, **_options):
        self.calls.append(list(args))
        path = args[-1]
        body, next_path = self.pages[path]
        text = json.dumps(body)
        etag = 'W/"' + hashlib.sha256(text.encode()).hexdigest() + '"'
        sent = args[args.index("-H") + 1].removeprefix("If-None-Match: ") if "-H" in args else None
        if sent == etag:
            return CommandResult(
                tuple(args), 1, f"HTTP/2.0 304 Not Modified\nEtag: {etag}\n\n", "gh: HTTP 304"
            )
        headers = [f"Etag: {etag}"]
        if next_path:
            headers.append(f'Link: <https://api.github.com/{next_path}>; rel="next"')
        head = "HTTP/2.0 200 OK\n" + "\n".join(headers)
        return CommandResult(tuple(args), 0, f"{head}\n\n{text}", "")


class FullLastPageTests(unittest.TestCase):
    FIRST = "repos/acme/widget/issues?state=all&per_page=2&sort=created&direction=asc"
    SECOND = "repositories/1/issues?state=all&per_page=2&after=abc"

    def test_an_issue_on_a_new_page_after_a_full_last_page_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "rest_cache.json"
            gh = GitHubPages({self.FIRST: ([{"number": 1}, {"number": 2}], None)})
            reader = ConditionalReader(gh, cache)
            self.assertEqual([1, 2], [value["number"] for value in reader.collection(self.FIRST)])
            reader.save()

            # Issue 3 starts a second page. The first page's body, and so its
            # ETag, is unchanged; only its Link header gained "next".
            gh.pages[self.FIRST] = ([{"number": 1}, {"number": 2}], self.SECOND)
            gh.pages[self.SECOND] = ([{"number": 3}], None)

            values = ConditionalReader(gh, cache).collection(self.FIRST)

        self.assertEqual([1, 2, 3], [value["number"] for value in values])

    def test_a_partial_last_page_is_still_revalidated_for_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "rest_cache.json"
            gh = GitHubPages({self.FIRST: ([{"number": 1}], None)})
            reader = ConditionalReader(gh, cache)
            reader.collection(self.FIRST)
            reader.save()

            again = ConditionalReader(gh, cache)
            again.collection(self.FIRST)

        self.assertEqual(1, again.stats.not_modified)
        self.assertTrue(all("-H" in call for call in gh.calls[1:]))


def project_item(number: int, item_id: str, *, project_id: str = "PVT_1") -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "ISSUE",
        "isArchived": False,
        "updatedAt": "2026-01-02T00:00:00Z",
        "project": {"id": project_id},
        "fieldValues": {
            "nodes": [{"name": "In progress", "field": {"id": "field-status", "name": "Status"}}]
        },
        "content": {
            "id": f"I_{number}",
            "number": number,
            "title": f"Request {number}",
            "body": f"<!-- kanbanlan:id={IDENTITY} -->",
            "url": f"https://github.com/{REPOSITORY}/issues/{number}",
            "state": "OPEN",
            "labels": {"nodes": [{"name": "status:in-progress", "color": "ededed"}]},
            "assignees": {"nodes": []},
            "comments": {
                "totalCount": 1,
                "nodes": [
                    {
                        "body": "CLAIM: 2026-01-02T00:00:00Z\nSession: wf-103",
                        "createdAt": "2026-01-02T00:00:00Z",
                        "author": {"login": "monalisa"},
                    }
                ],
            },
            "repository": {"nameWithOwner": REPOSITORY},
        },
    }


class OmittingProject(GitHub):
    """A Project whose items connection leaves out issue #103.

    That is what GitHub returned on 2026-11-04 while issue #103's own
    ``projectItems`` listed it on the Project, not archived.
    """

    def __init__(self, issues: list[dict[str, Any]], issue_side: list[dict[str, Any]]):
        super().__init__(Path("/tmp"), config())
        self.issues = issues
        self.issue_side = issue_side
        self.lookups: list[list[str]] = []

    def _fetch_project(self):
        listed = project_item(102, "PVTI_102")
        listed.pop("project")
        project = {
            "id": "PVT_1",
            "number": 2,
            "title": "Delivery",
            "url": "url",
            "repositories": {"nodes": [{"nameWithOwner": REPOSITORY}]},
            "fields": {"nodes": []},
            "items": [listed],
        }
        return project, {"remaining": 4000, "resetAt": "2026-01-01T00:00:00Z"}

    def _fetch_pull_requests(self, repository=None):
        return [], {"remaining": 4000, "resetAt": "2026-01-01T00:00:00Z"}

    def _repository_issues(self, repository):
        return self.issues

    def graphql(self, query, variables, **_options):
        self.lookups.append(list(variables["ids"]))
        return {
            "nodes": [{"id": "I_103", "projectItems": {"nodes": self.issue_side}}],
            "rateLimit": {"cost": 1, "remaining": 3999, "resetAt": "2026-01-01T00:00:00Z"},
        }


class OmittedProjectItemTests(unittest.TestCase):
    ISSUES = [
        rest_issue(102, labels=["status:in-progress"]),
        rest_issue(103, labels=["status:in-progress"]),
        rest_issue(104, body="not a request"),
        rest_issue(105, state="closed"),
    ]

    def test_an_open_request_the_items_connection_left_out_is_read_from_the_issue(self) -> None:
        github = OmittingProject(self.ISSUES, [project_item(103, "PVTI_103")])

        snapshot = github.snapshot(generated_at=datetime.now(UTC))

        by_number = {value["number"]: value for value in snapshot["items"]}
        self.assertEqual([102, 103], sorted(by_number))
        self.assertEqual("PVTI_103", by_number[103]["project_item_id"])
        self.assertEqual("In progress", by_number[103]["status"])
        self.assertEqual("wf-103", by_number[103]["active_claim"]["session"])
        # Only the open request missing from the read is looked up.
        self.assertEqual([["I_103"]], github.lookups)

    def test_an_item_on_another_project_is_not_added(self) -> None:
        github = OmittingProject(
            self.ISSUES, [project_item(103, "PVTI_OTHER", project_id="PVT_OTHER")]
        )

        snapshot = github.snapshot(generated_at=datetime.now(UTC))

        self.assertEqual([102], [value["number"] for value in snapshot["items"]])

    def test_a_complete_read_looks_nothing_up(self) -> None:
        github = OmittingProject([rest_issue(102, labels=["status:in-progress"])], [])

        github.snapshot(generated_at=datetime.now(UTC))

        self.assertEqual([], github.lookups)

    def test_an_unreadable_issue_list_leaves_the_project_read_as_it_was(self) -> None:
        github = OmittingProject(self.ISSUES, [])
        runner = mock.Mock()
        runner.run.return_value = CommandResult((), 1, "", "gh: Not Found (HTTP 404)")
        github.runner = runner
        with mock.patch("kanbanlan.github.cache_dir", side_effect=OSError):
            self.assertIsNone(GITHUB_REAL_REPOSITORY_ISSUES(github, REPOSITORY))
        github._repository_issues = lambda repository: None  # type: ignore[method-assign]

        snapshot = github.snapshot(generated_at=datetime.now(UTC))

        self.assertEqual([102], [value["number"] for value in snapshot["items"]])
        self.assertEqual([], github.lookups)

    def test_the_issue_list_is_the_queue_reads(self) -> None:
        self.assertEqual(
            "repos/acme/widget/issues?state=all&per_page=100&sort=created&direction=asc",
            issues_path(REPOSITORY),
        )


class StartReadsClaimsLiveTests(VerbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_project([])
        plan = patch_cli("_plan_worktree_cleanup", return_value=[])
        plan.start()
        self.addCleanup(plan.stop)
        self.record_steps("capture", "triage", "claim")

    def test_a_claim_missing_from_the_local_view_and_overlap_still_refuses_start(self) -> None:
        # The local view and the Project overlap both lack the session's
        # card (#103 on 2026-11-04); a live read has it.
        self.write_snapshot([item(8, BETA, "Ready")])
        live = snapshot(
            [item(7, ALPHA, "In progress", claim={"session": "s1"}), item(8, BETA, "Ready")]
        )
        self.store.refresh = mock.Mock(return_value=live)  # type: ignore[method-assign]

        code, payload, _ = self.run_cli("start", "#8", "--session", "s1")

        self.assertEqual(1, code)
        self.assertIn("already owns", payload["error"]["message"])
        self.assertEqual([], self.steps)
        self.store.refresh.assert_called_once_with(self.provider)

    def test_a_live_read_that_fails_refuses_start(self) -> None:
        self.write_snapshot([item(8, BETA, "Ready")])
        self.store.refresh = mock.Mock(side_effect=RuntimeError("GitHub unavailable"))  # type: ignore[method-assign]

        code, _, _ = self.run_cli("start", "#8", "--session", "s1")

        self.assertEqual(1, code)
        self.assertEqual([], self.steps)


class RecordFallsBackToALiveReadTests(unittest.TestCase):
    def test_a_card_missing_from_the_local_view_is_read_live(self) -> None:
        card = item(103, ALPHA, "In progress")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = build_parser().parse_args(["record", "#103"])
            with (
                patch_cli("_root", return_value=root),
                mock.patch("kanbanlan.config.Config.load", return_value=config()),
                patch_cli("create_provider"),
                patch_cli("cache_dir", return_value=root / "cache"),
                patch_cli("_local_view", return_value=snapshot([])),
                patch_cli("_read_card", return_value=snapshot([card])) as read,
                mock.patch("sys.stdout"),
            ):
                code = _cmd_record(args)

            self.assertEqual(0, code)
            read.assert_called_once()
            self.assertTrue((root / "docs" / "kanbanlan" / "requests" / f"{ALPHA}.md").exists())


if __name__ == "__main__":
    unittest.main()
