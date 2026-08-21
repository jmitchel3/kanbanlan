from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kanbanlan.config import Config, cache_dir
from kanbanlan.identity import attach_kanbanlan_id, extract_kanbanlan_id
from kanbanlan.providers import ProviderCapabilities
from kanbanlan.runner import (
    CommandError,
    CommandResult,
    RateLimitError,
    Runner,
    is_rate_limit_failure,
)
from kanbanlan.scaffold import PRIORITY_LABELS, STATUS_LABELS
from kanbanlan.snapshot import SCOPE_REPOSITORY, build_snapshot

# The full per-item selection is shared between the paginated Project read and
# the targeted hydration query so both are guaranteed to produce identical raw
# nodes; the snapshot builder never learns which path fetched an item.
PROJECT_ITEM_FIELDS = """
          id
          type
          isArchived
          updatedAt
          fieldValues(first: 30) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                optionId
                field { ... on ProjectV2SingleSelectField { id name } }
              }
            }
          }
          content {
            ... on Issue {
              id
              number
              title
              body
              url
              state
              stateReason
              createdAt
              updatedAt
              closedAt
              labels(first: 50) { nodes { name color } }
              milestone { title }
              assignees(first: 20) { nodes { login } }
              comments(last: 100) {
                totalCount
                nodes { body createdAt author { login } }
              }
              repository { nameWithOwner }
            }
            ... on PullRequest {
              id
              number
              title
              url
              state
              isDraft
              createdAt
              updatedAt
              mergedAt
              repository { nameWithOwner }
            }
            ... on DraftIssue {
              id
              title
              body
              createdAt
              updatedAt
            }
          }
""".strip("\n")

# The probe requests only what a change diff needs. GraphQL prices a query by
# the node counts it asks for, so leaving out comments, labels, assignees, and
# field values is what makes probing a stable board nearly free.
PROBE_ITEM_FIELDS = """
          id
          type
          isArchived
          updatedAt
          content {
            ... on Issue {
              id
              number
              updatedAt
              repository { nameWithOwner }
            }
            ... on PullRequest {
              id
              number
              updatedAt
              repository { nameWithOwner }
            }
          }
""".strip("\n")

_PROJECT_PAGE_TEMPLATE = """
query($owner: String!, $number: Int!, $after: String) {
  OWNER(login: $owner) {
    projectV2(number: $number) {
      id
      number
      title
      url
      updatedAt
      repositories(first: 100) {
        pageInfo { hasNextPage }
        nodes { nameWithOwner }
      }
      fields(first: 50) {
        nodes {
          ... on ProjectV2Field {
            id
            name
            dataType
          }
          ... on ProjectV2SingleSelectField {
            id
            name
            dataType
            options {
              id
              name
              color
              description
            }
          }
        }
      }
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
ITEM_FIELDS
        }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

PROJECT_QUERY = _PROJECT_PAGE_TEMPLATE.replace("ITEM_FIELDS", PROJECT_ITEM_FIELDS)

PROJECT_PROBE_QUERY = _PROJECT_PAGE_TEMPLATE.replace("ITEM_FIELDS", PROBE_ITEM_FIELDS)

ITEM_HYDRATION_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProjectV2Item {
ITEM_FIELDS
    }
  }
  rateLimit { cost remaining resetAt }
}
""".replace("ITEM_FIELDS", PROJECT_ITEM_FIELDS)

PULL_REQUEST_QUERY = """
query($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(
      first: 100,
      after: $after,
      states: OPEN,
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        url
        repository { nameWithOwner }
        headRefName
        baseRefName
        isDraft
        mergeStateStatus
        createdAt
        updatedAt
        author { login }
        labels(first: 50) { nodes { name color } }
        closingIssuesReferences(first: 50) {
          nodes { number url repository { nameWithOwner } }
        }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

REPOSITORY_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    id
    nameWithOwner
    owner { __typename login }
    defaultBranchRef { name }
  }
}
"""

PROJECT_REPOSITORIES_QUERY = """
query($owner: String!, $number: Int!) {
  OWNER(login: $owner) {
    projectV2(number: $number) {
      id
      title
      url
      repositories(first: 100) {
        pageInfo { hasNextPage }
        nodes { nameWithOwner }
      }
    }
  }
}
"""

OWNER_QUERY = """
query($login: String!) {
  repositoryOwner(login: $login) { __typename id login }
}
"""

UPDATE_STATUS_FIELD = """
mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
  updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
    projectV2Field {
      ... on ProjectV2SingleSelectField {
        id
        name
        options { id name color description }
      }
    }
  }
}
"""

# The raw-node cache is purely advisory: it only decides which items are worth
# re-fetching, never what a snapshot contains, so losing or corrupting it can
# cost points but can never produce a wrong board.
ITEM_CACHE_SCHEMA_VERSION = 1
ITEM_CACHE_FILENAME = "project_items.json"
HYDRATION_BATCH_SIZE = 30
FULL_REFRESH_ENV = "KANBANLAN_FULL_REFRESH"

REQUIRED_STATUS_OPTIONS = [
    ("Inbox", "GRAY", "Captured but not yet ready"),
    ("Ready", "GREEN", "Scoped, checked for overlap, and unblocked"),
    ("In progress", "YELLOW", "Claimed by one active session"),
    ("Blocked", "RED", "Waiting on a dependency or decision"),
    ("In review", "BLUE", "Pull request is open"),
    ("Done", "PURPLE", "Delivered to the configured staging branch"),
]
STATUS_ALIASES = {
    "Inbox": {"Todo", "To do", "Backlog"},
    "In progress": {"In Progress", "Doing"},
    "In review": {"Review"},
}


@dataclass(frozen=True)
class ProjectRead:
    """One Project read plus the open pull requests its scope needed."""

    project: dict[str, Any]
    pull_requests: list[dict[str, Any]] = field(default_factory=list)
    rate_limit: dict[str, Any] = field(default_factory=dict)
    unavailable_repositories: list[dict[str, Any]] = field(default_factory=list)


def project_repositories(project: dict[str, Any]) -> set[str]:
    """Return every repository the Project already references.

    Both routes matter. A linked repository qualifies before it has any card,
    which is how a peer repository's first delivery is recognized. Item content
    qualifies a repository whose work is already on the board even when the
    Project link is absent.
    """

    repositories: set[str] = {
        node["nameWithOwner"]
        for node in project.get("repositories", {}).get("nodes", [])
        if node and node.get("nameWithOwner")
    }
    for item in project.get("items", []):
        content = item.get("content") or {}
        name = (content.get("repository") or {}).get("nameWithOwner")
        if name:
            repositories.add(name)
    return repositories


class GitHub:
    provider_name = "github"
    capabilities = ProviderCapabilities()

    def __init__(self, root: Path, config: Config | None = None, runner: Runner | None = None):
        self.root = root
        self.config = config
        self.runner = runner or Runner(root)

    @staticmethod
    def require_cli() -> None:
        if shutil.which("gh") is None:
            raise RuntimeError("GitHub CLI is required; install it from https://cli.github.com")

    def ensure_auth(self, hostname: str = "github.com", *, interactive: bool = True) -> None:
        self.require_cli()
        status = self.runner.run(
            ["gh", "auth", "status", "--active", "--hostname", hostname],
            check=False,
        )
        if status.returncode == 0:
            return
        if not interactive:
            raise CommandError(status)
        if not _is_auth_failure(status):
            raise CommandError(status)
        print("GitHub authentication is missing or expired; opening the browser login flow.")
        self.runner.run(
            [
                "gh",
                "auth",
                "login",
                "--hostname",
                hostname,
                "--git-protocol",
                "https",
                "--web",
                "--scopes",
                "project",
            ],
            capture=False,
            timeout=None,
        )

    def ensure_project_scope(
        self,
        owner: str,
        hostname: str = "github.com",
        *,
        owner_type: str | None = None,
        interactive: bool = True,
    ) -> None:
        probe_owner = "@me" if owner_type == "user" else owner
        probe = self.runner.run(
            [
                "gh",
                "project",
                "list",
                "--owner",
                probe_owner,
                "--limit",
                "1",
                "--format",
                "json",
            ],
            check=False,
            retry=True,
        )
        if probe.returncode == 0:
            return
        if not interactive:
            raise CommandError(probe)
        if not _is_missing_project_scope(probe):
            raise CommandError(probe)
        print("GitHub Project access needs approval; opening the scope authorization flow.")
        self.runner.run(
            ["gh", "auth", "refresh", "--hostname", hostname, "--scopes", "project"],
            capture=False,
            timeout=None,
        )
        self.runner.run(
            [
                "gh",
                "project",
                "list",
                "--owner",
                probe_owner,
                "--limit",
                "1",
                "--format",
                "json",
            ]
        )

    def graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        retry: bool = False,
    ) -> dict[str, Any]:
        """Run one GraphQL document.

        Callers pass ``retry=True`` only for reads. Mutations stay on a single
        attempt because a request that reached GitHub may have applied before
        the response was lost.
        """
        try:
            result = self.runner.run(
                ["gh", "api", "graphql", "--input", "-"],
                input_text=json.dumps({"query": query, "variables": variables}),
                retry=retry,
            )
        except CommandError as exc:
            if is_rate_limit_failure(exc.result):
                raise RateLimitError(str(exc)) from exc
            raise
        payload = json.loads(result.stdout)
        errors = payload.get("errors")
        if errors:
            detail = "; ".join(error.get("message", "GraphQL error") for error in errors)
            if any(error.get("type") == "RATE_LIMITED" for error in errors):
                raise RateLimitError(detail)
            raise RuntimeError(detail)
        return payload["data"]

    def repository_info(self, repository: str) -> dict[str, Any]:
        owner, repo = repository.split("/", 1)
        data = self.graphql(REPOSITORY_QUERY, {"owner": owner, "repo": repo}, retry=True)
        value = data.get("repository")
        if not value:
            raise RuntimeError(f"repository {repository} was not found")
        return value

    def detect_owner_type(self, owner: str) -> str:
        data = self.graphql(OWNER_QUERY, {"login": owner}, retry=True)
        value = data.get("repositoryOwner")
        if value and value.get("__typename") == "Organization":
            return "organization"
        if value and value.get("__typename") == "User":
            return "user"
        raise RuntimeError(f"GitHub owner {owner} was not found")

    def list_projects(self, owner: str) -> list[dict[str, Any]]:
        payload = self.runner.json(
            ["gh", "project", "list", "--owner", owner, "--limit", "100", "--format", "json"],
            retry=True,
        )
        if isinstance(payload, list):
            return payload
        return payload.get("projects", [])

    def create_project(self, owner: str, title: str) -> dict[str, Any]:
        return self.runner.json(
            [
                "gh",
                "project",
                "create",
                "--owner",
                owner,
                "--title",
                title,
                "--format",
                "json",
            ]
        )

    def copy_project(
        self,
        source_owner: str,
        source_number: int,
        target_owner: str,
        title: str,
    ) -> dict[str, Any]:
        return self.runner.json(
            [
                "gh",
                "project",
                "copy",
                str(source_number),
                "--source-owner",
                source_owner,
                "--target-owner",
                target_owner,
                "--title",
                title,
                "--format",
                "json",
            ]
        )

    def _repository(self, repository: str | None = None) -> str:
        return repository or self._config().repository

    def link_project(self, repository: str | None = None) -> None:
        config = self._config()
        self.runner.run(
            [
                "gh",
                "project",
                "link",
                str(config.project_number),
                "--owner",
                config.project_owner,
                "--repo",
                self._repository(repository),
            ]
        )

    def project_repository_bindings(self) -> dict[str, Any]:
        """Read only the Project identity and its linked repositories.

        Preflight runs before any request exists, so it must not pay for a full
        paginated item read.
        """

        config = self._config()
        query = PROJECT_REPOSITORIES_QUERY.replace(
            "OWNER", "organization" if config.project_owner_type == "organization" else "user"
        )
        data = self.graphql(
            query,
            {"owner": config.project_owner, "number": config.project_number},
            retry=True,
        )
        owner = data.get("organization" if config.project_owner_type == "organization" else "user")
        project = owner and owner.get("projectV2")
        if not project:
            raise RuntimeError(
                f"Project {config.project_owner}/{config.project_number} was not found"
            )
        return project

    def repository_labels(self, repository: str) -> set[str]:
        payload = self.runner.json(
            [
                "gh",
                "label",
                "list",
                "--repo",
                repository,
                "--limit",
                "500",
                "--json",
                "name",
            ],
            retry=True,
        )
        return {value["name"] for value in payload}

    def inspect_repository_target(self, repository: str) -> dict[str, Any]:
        """Report what a repository would need, changing nothing.

        A plan must be able to describe the work without doing any of it, so
        every mutation stays in ``prepare_repository_target``.
        """

        info = self.repository_info(repository)
        target = info["nameWithOwner"]
        project = self.project_repository_bindings()
        linked = {
            node["nameWithOwner"]
            for node in project.get("repositories", {}).get("nodes", [])
            if node and node.get("nameWithOwner")
        }
        required = set(STATUS_LABELS) | set(PRIORITY_LABELS)
        return {
            "repository": target,
            "already_linked": target in linked,
            "missing_labels": sorted(required - self.repository_labels(target)),
            "project_url": project.get("url"),
        }

    def prepare_repository_target(self, repository: str) -> dict[str, Any]:
        """Validate and prepare a repository before anything is created or moved.

        Everything that can fail is done first, so a failure leaves no
        half-created request behind.
        """

        config = self._config()
        inspection = self.inspect_repository_target(repository)
        target = inspection["repository"]
        if not inspection["already_linked"]:
            try:
                self.link_project(target)
            except (CommandError, RuntimeError) as exc:
                raise RuntimeError(
                    f"repository {target} could not be linked to Project "
                    f"{config.project_owner}/{config.project_number}: {exc}"
                ) from exc
        self.ensure_labels(target)
        return inspection

    def prepare_capture_target(self, repository: str) -> dict[str, Any]:
        return self.prepare_repository_target(repository)

    def transfer_request(
        self,
        reference: int | str,
        target: str,
        *,
        repository: str | None = None,
    ) -> str:
        """Move one issue to another repository, preserving its discussion."""

        result = self.runner.run(
            [
                "gh",
                "issue",
                "transfer",
                str(_issue_number(reference)),
                target,
                "--repo",
                self._repository(repository),
            ]
        )
        url = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if not url:
            raise RuntimeError(f"GitHub did not report the new location of {reference} in {target}")
        return url

    def project_metadata(self) -> dict[str, Any]:
        project, _ = self._fetch_project()
        return project

    def ensure_status_options(self) -> bool:
        project = self.project_metadata()
        status_field = _status_field(project)
        if not status_field:
            raise RuntimeError("Project has no single-select Status field")
        existing = status_field.get("options", [])
        exact_names = {option["name"] for option in existing}
        required_names = {name for name, _, _ in REQUIRED_STATUS_OPTIONS}
        if required_names.issubset(exact_names):
            return False

        remaining = list(existing)
        updated: list[dict[str, Any]] = []
        for name, color, description in REQUIRED_STATUS_OPTIONS:
            match = next((option for option in remaining if option["name"] == name), None)
            if match is None:
                aliases = STATUS_ALIASES.get(name, set())
                match = next((option for option in remaining if option["name"] in aliases), None)
            option = {"name": name, "color": color, "description": description}
            if match:
                option["id"] = match["id"]
                remaining.remove(match)
            updated.append(option)

        for option in remaining:
            updated.append(
                {
                    "id": option["id"],
                    "name": option["name"],
                    "color": option["color"],
                    "description": option.get("description", ""),
                }
            )
        self.graphql(UPDATE_STATUS_FIELD, {"field": status_field["id"], "options": updated})
        return True

    def ensure_labels(self, repository: str | None = None) -> None:
        target = self._repository(repository)
        for name, (color, description) in {**STATUS_LABELS, **PRIORITY_LABELS}.items():
            self.runner.run(
                [
                    "gh",
                    "label",
                    "create",
                    name,
                    "--repo",
                    target,
                    "--color",
                    color,
                    "--description",
                    description,
                    "--force",
                ]
            )

    def collect(self, *, scope: str = SCOPE_REPOSITORY) -> ProjectRead:
        """Read the Project once and the open pull requests each scope needs.

        Open pull requests are discovered across every repository the Project
        already references, whatever the scope, because a request here can be
        delivered by a pull request in a peer repository. Discovery never
        enumerates repositories owned by the account: a repository joins the
        read by already being on the board. A peer repository that cannot be
        read is reported instead of failing the whole read, because the
        configured repository still has usable state.
        """

        config = self._config()
        project, project_rate_limit = self._fetch_project()
        targets = sorted({config.repository, *project_repositories(project)})
        pull_requests: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
        rate_limits = [project_rate_limit]
        for target in targets:
            try:
                values, rate_limit = self._fetch_pull_requests(target)
            except RateLimitError:
                # Out of quota means every remaining target fails too; a
                # "successful" snapshot missing peer repositories would hide
                # exactly the cross-repository work overlap checks exist for.
                raise
            except (CommandError, RuntimeError) as exc:
                if target == config.repository:
                    raise
                unavailable.append({"repository": target, "error": str(exc)})
                continue
            pull_requests.extend(values)
            rate_limits.append(rate_limit)
        return ProjectRead(
            project=project,
            pull_requests=pull_requests,
            rate_limit=min(rate_limits, key=lambda value: value.get("remaining", 10**12)),
            unavailable_repositories=unavailable,
        )

    def fetch(self) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        read = self.collect()
        return read.project, read.pull_requests, read.rate_limit

    def snapshot(
        self,
        *,
        generated_at: datetime,
        scope: str = SCOPE_REPOSITORY,
    ) -> dict[str, Any]:
        read = self.collect(scope=scope)
        return build_snapshot(
            self._config(),
            read.project,
            read.pull_requests,
            read.rate_limit,
            generated_at=generated_at,
            scope=scope,
            unavailable_repositories=read.unavailable_repositories,
        )

    def _fetch_project(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fetch the raw Project, paying only for the items that changed.

        Every full item page is priced by GitHub as if every issue really had
        100 comments, so a stable board used to pay its worst-case cost on
        every refresh. Instead, a cheap probe reads only identity and
        ``updatedAt`` timestamps, unchanged items are reassembled from the
        advisory raw-node cache, and only new or changed items are hydrated in
        full. Any doubt about the cache or a hydration result falls back to
        the full fetch, so this path can only change cost, never the returned
        project. ``KANBANLAN_FULL_REFRESH=1`` forces the full fetch.
        """

        if not _full_refresh_forced():
            cached_items = self._load_item_cache()
            if cached_items is not None:
                result = self._fetch_project_incremental(cached_items)
                if result is not None:
                    project, rate_limit = result
                    self._store_item_cache(project)
                    return project, rate_limit
        project, rate_limit = self._fetch_project_full()
        self._store_item_cache(project)
        return project, rate_limit

    def _fetch_project_full(self) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata, items, rate_limits = self._paginate_project(PROJECT_QUERY)
        metadata["items"] = items
        return metadata, _min_rate_limit(rate_limits)

    def _fetch_project_incremental(
        self,
        cached_items: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Probe, diff against the cache, and hydrate only what changed.

        Returns None whenever the probe or hydration produces anything
        unexpected, which tells the caller to run the full fetch instead.
        Provider errors (rate limits, missing project) propagate exactly as
        they do on the full path.
        """

        metadata, probe_items, rate_limits = self._paginate_project(PROJECT_PROBE_QUERY)
        order: list[str] = []
        reused: dict[str, Any] = {}
        stale: list[str] = []
        for probe_node in probe_items:
            if not isinstance(probe_node, dict) or not probe_node.get("id"):
                return None
            item_id = probe_node["id"]
            order.append(item_id)
            entry = cached_items.get(item_id)
            if _cache_entry_reusable(entry, probe_node):
                reused[item_id] = entry["node"]
            else:
                stale.append(item_id)
        hydrated, hydration_limits = self._hydrate_items(stale)
        rate_limits.extend(hydration_limits)
        if hydrated is None:
            return None
        items: list[dict[str, Any]] = []
        for item_id in order:
            node = reused.get(item_id) if item_id in reused else hydrated.get(item_id)
            if node is None:
                return None
            items.append(node)
        metadata["items"] = items
        return metadata, _min_rate_limit(rate_limits)

    def _hydrate_items(
        self,
        item_ids: list[str],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Fetch full raw nodes for the given project item ids, in batches.

        Returns (None, rate_limits) on any surprise (a null node, an id or
        type mismatch), so a single odd item costs one full fetch instead of
        a wrong snapshot.
        """

        hydrated: dict[str, Any] = {}
        rate_limits: list[dict[str, Any]] = []
        for start in range(0, len(item_ids), HYDRATION_BATCH_SIZE):
            batch = item_ids[start : start + HYDRATION_BATCH_SIZE]
            payload = self.graphql(ITEM_HYDRATION_QUERY, {"ids": batch}, retry=True)
            rate_limits.append(payload.get("rateLimit", {}))
            nodes = payload.get("nodes")
            if not isinstance(nodes, list) or len(nodes) != len(batch):
                return None, rate_limits
            for requested_id, node in zip(batch, nodes):
                if not isinstance(node, dict) or node.get("id") != requested_id:
                    return None, rate_limits
                hydrated[requested_id] = node
        return hydrated, rate_limits

    def _paginate_project(
        self,
        query_template: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        config = self._config()
        query = query_template.replace(
            "OWNER", "organization" if config.project_owner_type == "organization" else "user"
        )
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        metadata: dict[str, Any] | None = None
        rate_limits: list[dict[str, Any]] = []
        while True:
            payload = self.graphql(
                query,
                {
                    "owner": config.project_owner,
                    "number": config.project_number,
                    "after": cursor,
                },
                retry=True,
            )
            owner = payload.get(
                "organization" if config.project_owner_type == "organization" else "user"
            )
            project = owner and owner.get("projectV2")
            if not project:
                raise RuntimeError(
                    f"Project {config.project_owner}/{config.project_number} was not found"
                )
            if metadata is None:
                metadata = {key: value for key, value in project.items() if key != "items"}
            connection = project["items"]
            items.extend(connection.get("nodes", []))
            rate_limit = payload.get("rateLimit")
            if rate_limit:
                rate_limits.append(rate_limit)
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]
        assert metadata is not None
        return metadata, items, rate_limits

    def _item_cache_path(self) -> Path | None:
        try:
            return cache_dir(self.root) / ITEM_CACHE_FILENAME
        except (CommandError, RuntimeError, OSError):
            return None

    def _item_cache_key(self) -> str:
        config = self._config()
        return f"{config.project_owner}/{config.project_number}"

    def _load_item_cache(self) -> dict[str, Any] | None:
        """Read the advisory raw-node cache, or None when it cannot be trusted.

        Missing, unreadable, corrupt, version-mismatched, or pointed at a
        different Project all mean the same thing: run the full fetch and
        rewrite the cache from its result.
        """

        path = self._item_cache_path()
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("schema_version") != ITEM_CACHE_SCHEMA_VERSION:
            return None
        if data.get("project") != self._item_cache_key():
            return None
        entries = data.get("items")
        if not isinstance(entries, dict):
            return None
        return entries

    def _store_item_cache(self, project: dict[str, Any]) -> None:
        """Rewrite the advisory cache from a freshly assembled project.

        The write is atomic (tempfile plus rename), so a concurrent reader,
        including a lock-free project-scope read, sees either the previous
        complete cache or this one, never a torn file. A failed write is
        ignored: it only makes the next refresh pay full price.
        """

        path = self._item_cache_path()
        if path is None:
            return
        entries: dict[str, Any] = {}
        for node in project.get("items", []):
            if not isinstance(node, dict) or not node.get("id"):
                continue
            entries[node["id"]] = {
                "item_updated_at": node.get("updatedAt"),
                "content_updated_at": (node.get("content") or {}).get("updatedAt"),
                "node": node,
            }
        payload = {
            "schema_version": ITEM_CACHE_SCHEMA_VERSION,
            "project": self._item_cache_key(),
            "items": entries,
        }
        try:
            _write_json_atomic(path, payload)
        except OSError:
            pass

    def _fetch_pull_requests(
        self,
        repository: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        target = repository or self._config().repository
        owner, repo = target.split("/", 1)
        values: list[dict[str, Any]] = []
        cursor: str | None = None
        rate_limit: dict[str, Any] = {}
        while True:
            payload = self.graphql(
                PULL_REQUEST_QUERY,
                {"owner": owner, "repo": repo, "after": cursor},
                retry=True,
            )
            repository_payload = payload.get("repository")
            if not repository_payload:
                raise RuntimeError(f"repository {target} was not found")
            connection = repository_payload["pullRequests"]
            values.extend(connection.get("nodes", []))
            rate_limit = payload.get("rateLimit", rate_limit)
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]
        return values, rate_limit

    def open_issues(self) -> list[dict[str, Any]]:
        config = self._config()
        payload = self.runner.json(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                config.repository,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,url,labels",
            ],
            retry=True,
        )
        return payload

    def list_open_requests(self) -> list[dict[str, Any]]:
        return self.open_issues()

    def add_issue_to_project(self, url: str) -> dict[str, Any]:
        config = self._config()
        return self.runner.json(
            [
                "gh",
                "project",
                "item-add",
                str(config.project_number),
                "--owner",
                config.project_owner,
                "--url",
                url,
                "--format",
                "json",
            ]
        )

    def add_to_projection(self, url: str) -> dict[str, Any]:
        return self.add_issue_to_project(url)

    def set_project_status(self, item_id: str, project: dict[str, Any], status: str) -> None:
        field = _status_field(project)
        if not field:
            raise RuntimeError("Project has no single-select Status field")
        option = next((value for value in field["options"] if value["name"] == status), None)
        if not option:
            raise RuntimeError(f"Project Status option {status!r} is missing")
        self.runner.run(
            [
                "gh",
                "project",
                "item-edit",
                "--id",
                item_id,
                "--project-id",
                project["id"],
                "--field-id",
                field["id"],
                "--single-select-option-id",
                option["id"],
            ]
        )

    def set_projection_status(
        self,
        item_id: str,
        projection: dict[str, Any],
        status: str,
    ) -> None:
        self.set_project_status(item_id, projection, status)

    def set_issue_status_label(
        self,
        number: int,
        label: str | None,
        *,
        repository: str | None = None,
    ) -> None:
        target = self._repository(repository)
        payload = self.runner.json(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                target,
                "--json",
                "labels",
            ],
            retry=True,
        )
        current = {
            value["name"]
            for value in payload.get("labels", [])
            if value["name"].startswith("status:")
        }
        wanted = {label} if label else set()
        if current == wanted:
            return
        args = [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            target,
        ]
        for value in sorted(current - wanted):
            args.extend(["--remove-label", value])
        for value in sorted(wanted - current):
            args.extend(["--add-label", value])
        self.runner.run(args)

    def set_request_status(
        self,
        reference: int | str,
        label: str | None,
        *,
        repository: str | None = None,
    ) -> None:
        self.set_issue_status_label(_issue_number(reference), label, repository=repository)

    def comment_issue(self, number: int, body: str, *, repository: str | None = None) -> None:
        self.runner.run(
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "--repo",
                self._repository(repository),
                "--body",
                body,
            ]
        )

    def comment_request(
        self,
        reference: int | str,
        body: str,
        *,
        repository: str | None = None,
    ) -> None:
        self.comment_issue(_issue_number(reference), body, repository=repository)

    def create_issue(
        self,
        title: str,
        body: str,
        priority: str,
        *,
        repository: str | None = None,
    ) -> str:
        result = self.runner.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                self._repository(repository),
                "--title",
                title,
                "--body",
                body,
                "--label",
                "status:intake",
                "--label",
                priority,
            ]
        )
        return result.stdout.strip()

    def create_request(
        self,
        title: str,
        body: str,
        priority: str,
        *,
        repository: str | None = None,
    ) -> str:
        return self.create_issue(title, body, priority, repository=repository)

    def ensure_request_identity(
        self,
        reference: int | str,
        kanbanlan_id: str,
        *,
        repository: str | None = None,
    ) -> str:
        number = _issue_number(reference)
        target = self._repository(repository)
        payload = self.runner.json(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                target,
                "--json",
                "body",
            ],
            retry=True,
        )
        body = payload.get("body") or ""
        existing = extract_kanbanlan_id(body)
        if existing:
            return existing
        updated = attach_kanbanlan_id(body, kanbanlan_id)
        self.runner.run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "--repo",
                target,
                "--body",
                updated,
            ]
        )
        return kanbanlan_id

    def open_project(self) -> None:
        config = self._config()
        self.runner.run(
            [
                "gh",
                "project",
                "view",
                str(config.project_number),
                "--owner",
                config.project_owner,
                "--web",
            ],
            capture=False,
            timeout=None,
        )

    def _config(self) -> Config:
        if self.config is None:
            raise RuntimeError("this GitHub operation requires repository configuration")
        return self.config


def _full_refresh_forced() -> bool:
    value = os.environ.get(FULL_REFRESH_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _cache_entry_reusable(entry: Any, probe_node: dict[str, Any]) -> bool:
    """Decide whether one cached raw node can stand in for a live item.

    Both timestamps must match because they move independently: a Status
    field edit bumps the item's ``updatedAt`` but not the issue's, while a
    new comment or label bumps the issue's ``updatedAt`` but not necessarily
    the item's. A probe node without both timestamps (a draft issue, a
    redacted item, anything unexpected) is never reused, so doubt always
    means rehydrate.
    """

    if not isinstance(entry, dict) or not isinstance(entry.get("node"), dict):
        return False
    item_updated = probe_node.get("updatedAt")
    content_updated = (probe_node.get("content") or {}).get("updatedAt")
    if not item_updated or not content_updated:
        return False
    return (
        entry.get("item_updated_at") == item_updated
        and entry.get("content_updated_at") == content_updated
    )


def _min_rate_limit(rate_limits: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [value for value in rate_limits if value]
    if not candidates:
        return {}
    return min(candidates, key=lambda value: value.get("remaining", 10**12))


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Write via a tempfile and rename, mirroring the snapshot cache writes.

    Readers of the advisory cache run without the refresh lock, so the rename
    is what guarantees they only ever see a complete document.
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as file:
            temporary_path = file.name
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _status_field(project: dict[str, Any]) -> dict[str, Any] | None:
    for value in project.get("fields", {}).get("nodes", []):
        if value and value.get("name") == "Status" and "options" in value:
            return value
    return None


def _issue_number(reference: int | str) -> int:
    try:
        number = int(str(reference).removeprefix("#"))
    except ValueError as exc:
        raise RuntimeError(
            f"GitHub request reference must be an issue number: {reference!r}"
        ) from exc
    if number < 1:
        raise RuntimeError("GitHub issue number must be positive")
    return number


def _command_detail(result: CommandResult) -> str:
    return f"{result.stdout}\n{result.stderr}".lower()


def _is_auth_failure(result: CommandResult) -> bool:
    detail = _command_detail(result)
    return any(
        marker in detail
        for marker in (
            "not logged into",
            "not logged in",
            "token is invalid",
            "token in default is invalid",
            "authentication failed",
            "no oauth token",
        )
    )


def _is_missing_project_scope(result: CommandResult) -> bool:
    detail = _command_detail(result)
    return any(
        marker in detail
        for marker in (
            "missing required scopes",
            "requires the project scope",
            "requires the read:project scope",
            "insufficient oauth scope",
        )
    )
