from __future__ import annotations

import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from kanbanlan.cli import build_parser
from kanbanlan.config import Config
from kanbanlan.domain import resolve_request_item
from kanbanlan.identity import (
    attach_kanbanlan_id,
    extract_kanbanlan_id,
    new_kanbanlan_id,
    strip_kanbanlan_metadata,
)
from kanbanlan.records import create_record
from kanbanlan.snapshot import CacheStore, build_snapshot
from kanbanlan.workflow import apply_reconciliation, plan_reconciliation

KANBANLAN_ID = "KBL-ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def config() -> Config:
    return Config(
        repository="acme/widget",
        project_owner="acme",
        project_owner_type="organization",
        project_number=2,
    )


def raw_issue(body: str, *, number: int = 7) -> dict:
    return {
        "id": f"item-{number}",
        "type": "ISSUE",
        "isArchived": False,
        "fieldValues": {"nodes": [{"name": "Ready", "field": {"name": "Status"}}]},
        "content": {
            "id": f"issue-{number}",
            "number": number,
            "title": "Portable request",
            "body": body,
            "url": f"https://github.test/acme/widget/issues/{number}",
            "state": "OPEN",
            "stateReason": None,
            "createdAt": "2026-07-25T00:00:00Z",
            "updatedAt": "2026-07-25T01:00:00Z",
            "closedAt": None,
            "repository": {"nameWithOwner": "acme/widget"},
            "labels": {
                "nodes": [
                    {"name": "status:ready", "color": "000000"},
                    {"name": "priority:p1", "color": "000000"},
                ]
            },
            "assignees": {"nodes": []},
            "comments": {"nodes": []},
        },
    }


def project(body: str) -> dict:
    return {
        "id": "project",
        "number": 2,
        "title": "Delivery",
        "url": "https://github.test/projects/2",
        "fields": {"nodes": []},
        "items": [raw_issue(body)],
    }


class IdentityTests(unittest.TestCase):
    def test_generated_identity_is_provider_independent_and_round_trips(self) -> None:
        first = new_kanbanlan_id()
        second = new_kanbanlan_id()

        self.assertRegex(first, r"^KBL-[A-Z2-7]{26}$")
        self.assertNotEqual(first, second)
        body = attach_kanbanlan_id("## Outcome\n\nShip it.", first)
        self.assertEqual(first, extract_kanbanlan_id(body))
        self.assertEqual("## Outcome\n\nShip it.", strip_kanbanlan_metadata(body))
        self.assertEqual(body, attach_kanbanlan_id(body, first))
        self.assertIsNone(extract_kanbanlan_id(f"Blocked by {second}"))

    def test_snapshot_has_portable_identity_and_resolves_aliases(self) -> None:
        body = attach_kanbanlan_id("Request body", KANBANLAN_ID)
        pull_request = {
            "number": 11,
            "title": "Implementation",
            "body": f"Kanbanlan: {KANBANLAN_ID}",
            "url": "https://github.test/pull/11",
            "labels": {"nodes": []},
            "closingIssuesReferences": {"nodes": []},
        }
        snapshot = build_snapshot(
            config(),
            project(body),
            [pull_request],
            {},
            datetime(2026, 7, 25, tzinfo=UTC),
        )
        item = snapshot["items"][0]

        self.assertEqual(2, snapshot["schema_version"])
        self.assertEqual(KANBANLAN_ID, item["kanbanlan_id"])
        self.assertEqual("github:acme/widget#7", item["provider_ref"])
        self.assertEqual(11, item["linked_open_pull_requests"][0]["number"])
        self.assertIs(item, resolve_request_item(snapshot, KANBANLAN_ID.lower()))
        self.assertIs(item, resolve_request_item(snapshot, "#7"))
        self.assertIs(item, resolve_request_item(snapshot, "github:acme/widget#7"))


class ProviderContractTests(unittest.TestCase):
    def test_duplicate_identities_are_reported_without_automatic_replacement(self) -> None:
        body = attach_kanbanlan_id("Request body", KANBANLAN_ID)
        value = project(body)
        value["items"].append(raw_issue(body, number=8))
        snapshot = build_snapshot(
            config(),
            value,
            [],
            {},
            datetime(2026, 7, 25, tzinfo=UTC),
        )

        planned = plan_reconciliation(snapshot, [])

        duplicates = [value for value in planned if value.kind == "duplicate_kanbanlan_id"]
        self.assertEqual([7, 8], [value.issue_number for value in duplicates])

    def test_reconciliation_backfills_missing_identity_through_provider_contract(self) -> None:
        class Provider:
            provider_name = "fake"

            def __init__(self) -> None:
                self.body = "Request body"

            def snapshot(self, *, generated_at):
                return build_snapshot(config(), project(self.body), [], {}, generated_at)

            def list_open_requests(self):
                return [{"number": 7, "url": "https://github.test/issues/7"}]

            def ensure_request_identity(self, reference, kanbanlan_id):
                self.body = attach_kanbanlan_id(self.body, kanbanlan_id)
                return kanbanlan_id

            def add_to_projection(self, _url):
                raise AssertionError("request is already projected")

            def set_request_status(self, _reference, _label):
                raise AssertionError("status is already correct")

            def set_projection_status(self, _item_id, _projection, _status):
                raise AssertionError("projection status is already correct")

        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            snapshot = store.refresh(provider)
            planned = plan_reconciliation(snapshot, provider.list_open_requests())
            remaining, refreshed = apply_reconciliation(
                provider,
                store,
                snapshot,
                provider.list_open_requests(),
            )

        self.assertEqual(["assign_kanbanlan_id"], [value.kind for value in planned])
        self.assertEqual([], remaining)
        self.assertRegex(refreshed["items"][0]["kanbanlan_id"], r"^KBL-[A-Z2-7]{26}$")


class RepositoryRecordTests(unittest.TestCase):
    def test_record_is_created_once_and_preserves_manual_edits(self) -> None:
        snapshot = build_snapshot(
            config(),
            project(attach_kanbanlan_id("## Outcome\n\nShip it.", KANBANLAN_ID)),
            [],
            {},
            datetime(2026, 7, 25, tzinfo=UTC),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = create_record(root, snapshot["items"][0])
            created.path.write_text("manual\n", encoding="utf-8")
            repeated = create_record(root, snapshot["items"][0])

            self.assertEqual("created", created.action)
            self.assertEqual("unchanged", repeated.action)
            self.assertEqual("manual\n", created.path.read_text(encoding="utf-8"))
            self.assertTrue(re.search(r"docs/kanbanlan/requests/KBL-.+\.md$", str(created.path)))


class CompatibilityTests(unittest.TestCase):
    def test_schema_v1_snapshot_is_stale_even_when_recent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CacheStore(config(), Path(directory))
            store._write_json(
                store.snapshot_path,
                {
                    "schema_version": 1,
                    "generated_at": "2999-01-01T00:00:00Z",
                },
            )

            self.assertFalse(store.is_fresh())

    def test_schema_v1_config_loads_as_github_and_writes_schema_v2(self) -> None:
        legacy = """schema_version = 1

[repository]
name_with_owner = "acme/widget"

[project]
owner = "acme"
owner_type = "organization"
number = 2
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".kanbanlan.toml").write_text(legacy, encoding="utf-8")
            loaded = Config.load(root)

        self.assertEqual("github", loaded.code_host)
        self.assertEqual("github", loaded.canonical_home)
        self.assertEqual(("github_projects",), loaded.projections)
        self.assertTrue(loaded.to_toml().startswith("schema_version = 2\n"))

    def test_cli_accepts_portable_request_references_and_json_mode(self) -> None:
        args = build_parser().parse_args(["--json", "claim", KANBANLAN_ID, "--touchpoints", "src"])
        self.assertTrue(args.json_output)
        self.assertEqual(KANBANLAN_ID, args.issue)
