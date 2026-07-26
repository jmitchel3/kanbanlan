from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kanbanlan.config import (
    Config,
    cache_dir,
    discover_default_branch,
    discover_repository,
    find_repo_root,
)
from kanbanlan.github import REQUIRED_STATUS_OPTIONS, GitHub
from kanbanlan.runner import CommandError, Runner
from kanbanlan.scaffold import PRIORITY_LABELS, STATUS_LABELS, scaffold_repository
from kanbanlan.snapshot import CacheStore
from kanbanlan.workflow import (
    apply_reconciliation,
    format_drift,
    plan_reconciliation,
)

PROJECT_URL_RE = re.compile(r"github\.com/(?:orgs|users)/(?P<owner>[^/]+)/projects/(?P<number>\d+)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kanbanlan",
        description="Reusable GitHub Project coordination for humans and coding agents.",
    )
    parser.add_argument(
        "-C",
        "--repo-root",
        help="run against this Git repository instead of the current directory",
    )
    parser.add_argument("--version", action="version", version="kanbanlan 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="configure a repository and GitHub Project")
    init.add_argument("--repository", help="GitHub owner/repository; defaults from origin")
    init.add_argument("--project-url", help="existing GitHub Project URL")
    init.add_argument("--project-owner", help="Project owner; defaults to repository owner")
    init.add_argument("--project-number", type=int, help="existing Project number")
    init.add_argument(
        "--owner-type",
        choices=("organization", "user"),
        help="Project owner type; normally auto-detected",
    )
    init.add_argument("--create-project", action="store_true", help="create a new Project")
    init.add_argument(
        "--template-project",
        metavar="OWNER/NUMBER",
        help="copy this Project instead of creating an empty Project",
    )
    init.add_argument("--project-title", help="title for a newly created/copied Project")
    init.add_argument("--default-branch", help="delivery branch; defaults from origin")
    init.add_argument("--stage-branch", help="branch deployed to staging")
    init.add_argument("--production-branch", default="", help="optional production branch")
    init.add_argument("--hostname", default="github.com")
    init.add_argument("--stale-seconds", type=int, default=180)
    init.add_argument("--force", action="store_true", help="replace custom generated targets")
    init.add_argument("--non-interactive", action="store_true")
    init.add_argument(
        "--local-only",
        action="store_true",
        help="write repository files without GitHub mutations",
    )
    init.add_argument(
        "--skip-reconcile",
        action="store_true",
        help="do not add/reconcile existing open issues during setup",
    )
    init.add_argument("--open", action="store_true", help="open the Project in a browser")

    commands.add_parser("auth", help="repair GitHub login and Project scope")
    commands.add_parser("doctor", help="check local config, auth, fields, and labels")
    commands.add_parser("ensure", help="ensure the shared snapshot is fresh")
    commands.add_parser("refresh", help="refresh the shared snapshot now")
    commands.add_parser("status", help="show local cache and board summary")
    commands.add_parser("snapshot", help="print the current snapshot JSON")
    commands.add_parser("path", help="print the shared snapshot path")
    commands.add_parser("next", help="show the first unblocked Ready card")

    reconcile = commands.add_parser("reconcile", help="report label/claim/PR/Project drift")
    reconcile.add_argument("--apply", action="store_true", help="apply the displayed repairs")

    capture = commands.add_parser("capture", help="create an Inbox request card")
    capture.add_argument("title")
    capture.add_argument("--body", default="")
    capture.add_argument(
        "--priority",
        choices=tuple(PRIORITY_LABELS),
        default="priority:p2",
    )

    claim = commands.add_parser("claim", help="claim one Ready issue")
    claim.add_argument("issue", type=int)
    claim.add_argument("--touchpoints", required=True)
    claim.add_argument("--session")
    claim.add_argument("--branch")
    claim.add_argument("--worktree")
    claim.add_argument(
        "--no-worktree",
        action="store_true",
        help="claim using the current branch/worktree instead of creating one",
    )

    release = commands.add_parser("release", help="release an active claim")
    release.add_argument("issue", type=int)
    release.add_argument("--reason", required=True)
    release.add_argument("--blocked", action="store_true")

    review = commands.add_parser("review", help="move an issue with an open PR to review")
    review.add_argument("issue", type=int)

    handoff = commands.add_parser("handoff", help="transfer an active claim")
    handoff.add_argument("issue", type=int)
    handoff.add_argument("--session", required=True, help="new owner/session identifier")
    handoff.add_argument("--branch", required=True)
    handoff.add_argument("--worktree", required=True)
    handoff.add_argument("--reason", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        handler = globals()[f"_cmd_{args.command.replace('-', '_')}"]
        return int(handler(args) or 0)
    except (CommandError, RuntimeError, ValueError) as exc:
        print(f"kanbanlan: {exc}", file=sys.stderr)
        return 1


def _root(args: argparse.Namespace) -> Path:
    start = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    return find_repo_root(start)


def _context(args: argparse.Namespace) -> tuple[Path, Config, GitHub, CacheStore]:
    root = _root(args)
    config = Config.load(root)
    github = GitHub(root, config)
    store = CacheStore(config, cache_dir(root))
    return root, config, github, store


def _cmd_init(args: argparse.Namespace) -> int:
    root = _root(args)
    repository = args.repository or discover_repository(root)
    repo_owner, repo_name = repository.split("/", 1)
    project_owner, project_number = _project_reference(args, repo_owner)
    default_branch = args.default_branch or discover_default_branch(root)
    github = GitHub(root)

    if args.local_only:
        if not project_number:
            raise RuntimeError("--local-only requires --project-number or --project-url")
        owner_type = args.owner_type or "organization"
    else:
        github.ensure_auth(args.hostname, interactive=not args.non_interactive)
        repository_info = github.repository_info(repository)
        default_branch = (
            args.default_branch
            or (repository_info.get("defaultBranchRef") or {}).get("name")
            or default_branch
        )
        project_owner = project_owner or repository_info["owner"]["login"]
        github.ensure_project_scope(
            project_owner,
            args.hostname,
            interactive=not args.non_interactive,
        )
        owner_type = args.owner_type or github.detect_owner_type(project_owner)
        if not project_number:
            project_number = _choose_or_create_project(
                args,
                github,
                project_owner,
                repo_name,
            )

    assert project_owner
    assert project_number
    config = Config(
        repository=repository,
        project_owner=project_owner,
        project_owner_type=owner_type,
        project_number=project_number,
        default_branch=default_branch,
        stage_branch=args.stage_branch or default_branch,
        production_branch=args.production_branch,
        hostname=args.hostname,
        stale_seconds=args.stale_seconds,
    )
    results = scaffold_repository(root, config, force=args.force)
    for result in results:
        print(f"{result.action}: {result.path.relative_to(root)}")

    if args.local_only:
        print("Local setup complete; run 'kanbanlan doctor' when GitHub access is available.")
        return 0

    github.config = config
    github.link_project()
    changed = github.ensure_status_options()
    print("updated: Project Status options" if changed else "unchanged: Project Status options")
    github.ensure_labels()
    print("updated: repository workflow labels")

    store = CacheStore(config, cache_dir(root))
    snapshot = store.refresh(github)
    if not args.skip_reconcile:
        drift = plan_reconciliation(snapshot, github.open_issues())
        if drift:
            print(f"reconciling {len(drift)} existing issue/Project differences")
            remaining, _ = apply_reconciliation(
                github,
                store,
                snapshot,
                github.open_issues(),
            )
            if remaining:
                raise RuntimeError(
                    f"{len(remaining)} reconciliation differences remain after setup"
                )
    if args.open:
        github.open_project()
    print(f"Kanbanlan configured for {repository} and Project {project_owner}/{project_number}.")
    return 0


def _project_reference(
    args: argparse.Namespace,
    repo_owner: str,
) -> tuple[str, int | None]:
    owner = args.project_owner or repo_owner
    number = args.project_number
    if args.project_url:
        match = PROJECT_URL_RE.search(args.project_url)
        if not match:
            raise RuntimeError("--project-url is not a GitHub Projects v2 URL")
        url_owner = match.group("owner")
        if args.project_owner and args.project_owner != url_owner:
            raise RuntimeError("--project-owner conflicts with --project-url")
        owner = url_owner
        number = int(match.group("number"))
    return owner, number


def _choose_or_create_project(
    args: argparse.Namespace,
    github: GitHub,
    owner: str,
    repo_name: str,
) -> int:
    title = args.project_title or f"{repo_name} Delivery"
    if args.template_project:
        source_owner, source_number = _parse_template(args.template_project)
        created = github.copy_project(source_owner, source_number, owner, title)
        return _project_number(created)
    if args.create_project:
        return _project_number(github.create_project(owner, title))

    projects = github.list_projects(owner)
    if args.non_interactive:
        if len(projects) == 1:
            return int(projects[0]["number"])
        raise RuntimeError(
            "choose --project-number, --project-url, --create-project, "
            "or --template-project in non-interactive mode"
        )
    print("Available GitHub Projects:")
    for project in projects:
        print(f"  {project['number']}: {project['title']}")
    response = input("Project number, or 'new' to create one: ").strip()
    if response.lower() == "new":
        return _project_number(github.create_project(owner, title))
    try:
        return int(response)
    except ValueError as exc:
        raise RuntimeError("a numeric Project number or 'new' is required") from exc


def _parse_template(value: str) -> tuple[str, int]:
    try:
        owner, raw_number = value.rsplit("/", 1)
        return owner, int(raw_number)
    except ValueError as exc:
        raise RuntimeError("--template-project must use OWNER/NUMBER") from exc


def _project_number(payload: dict[str, Any]) -> int:
    if payload.get("number"):
        return int(payload["number"])
    match = re.search(r"/projects/(\d+)", payload.get("url", ""))
    if match:
        return int(match.group(1))
    raise RuntimeError("GitHub did not return the new Project number")


def _cmd_auth(args: argparse.Namespace) -> int:
    _, config, github, _ = _context(args)
    github.ensure_auth(config.hostname, interactive=True)
    github.ensure_project_scope(config.project_owner, config.hostname, interactive=True)
    print("GitHub authentication and Project scope are ready.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    root, config, github, store = _context(args)
    failures: list[str] = []
    print(f"config: {root / '.kanbanlan.toml'}")
    print(f"repository: {config.repository}")
    print(f"project: {config.project_owner}/{config.project_number}")

    auth = github.runner.run(
        ["gh", "auth", "status", "--active", "--hostname", config.hostname],
        check=False,
    )
    if auth.returncode:
        failures.append("GitHub authentication is unavailable; run 'kanbanlan auth'")
    else:
        print("auth: ok")
        try:
            project = github.project_metadata()
            names = {
                option["name"]
                for field in project.get("fields", {}).get("nodes", [])
                if field and field.get("name") == "Status"
                for option in field.get("options", [])
            }
            missing = {name for name, _, _ in REQUIRED_STATUS_OPTIONS} - names
            if missing:
                failures.append(f"Project Status options missing: {', '.join(sorted(missing))}")
            else:
                print("Project Status field: ok")
            labels = github.runner.json(
                [
                    "gh",
                    "label",
                    "list",
                    "--repo",
                    config.repository,
                    "--limit",
                    "200",
                    "--json",
                    "name",
                ]
            )
            names = {value["name"] for value in labels}
            missing_labels = (set(STATUS_LABELS) | set(PRIORITY_LABELS)) - names
            if missing_labels:
                failures.append(f"labels missing: {', '.join(sorted(missing_labels))}")
            else:
                print("workflow labels: ok")
        except (CommandError, RuntimeError) as exc:
            failures.append(str(exc))
    print(f"cache: {store.inspect()['snapshot_state']} ({store.snapshot_path})")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("doctor: all checks passed")
    return 0


def _cmd_ensure(args: argparse.Namespace) -> int:
    _, _, github, store = _context(args)
    store.ensure(github)
    print(store.snapshot_path)
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    _, _, github, store = _context(args)
    snapshot = store.refresh(github)
    print(f"refreshed {store.snapshot_path} at {snapshot['generated_at']}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _, _, _, store = _context(args)
    status = store.inspect()
    print(f"snapshot: {status['snapshot_state']}")
    if status["generated_at"]:
        print(f"generated: {status['generated_at']} ({status['age_seconds']:.0f}s ago)")
    counts = status["status_counts"]
    print("board:", ", ".join(f"{name}={count}" for name, count in counts.items()) or "empty")
    next_ready = status["next_ready"]
    if next_ready:
        print(f"next: #{next_ready['number']} {next_ready['title']}")
    if status["error"]:
        print(f"last error: {status['error']['kind']}: {status['error']['message']}")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    _, _, _, store = _context(args)
    snapshot = store.snapshot()
    if snapshot is None:
        raise RuntimeError("no snapshot is available; run 'kanbanlan ensure'")
    json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    _, _, _, store = _context(args)
    print(store.snapshot_path)
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    _, _, github, store = _context(args)
    snapshot = store.ensure(github)
    item = snapshot.get("next_ready")
    if not item:
        print("No unblocked Ready card is available.")
        return 0
    priority = item.get("priority") or "unprioritized"
    print(f"#{item['number']} [{priority}] {item['title']}")
    print(item["url"])
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    _, _, github, store = _context(args)
    snapshot = store.refresh(github)
    open_issues = github.open_issues()
    drift = plan_reconciliation(snapshot, open_issues)
    if not drift:
        print("GitHub Issues and Project Status are reconciled.")
        return 0
    for value in drift:
        print(format_drift(value))
    if not args.apply:
        print(f"{len(drift)} difference(s); rerun with --apply to repair them.")
        return 2
    remaining, _ = apply_reconciliation(github, store, snapshot, open_issues)
    if remaining:
        for value in remaining:
            print(f"remaining: {format_drift(value)}")
        return 1
    print("Reconciliation applied and verified.")
    return 0


def _cmd_capture(args: argparse.Namespace) -> int:
    _, _, github, store = _context(args)
    body = args.body or (
        "## Outcome\n\n"
        "<!-- Describe the independently reviewable result. -->\n\n"
        "## Acceptance criteria\n\n- [ ] "
    )
    url = github.create_issue(args.title, body, args.priority)
    github.add_issue_to_project(url)
    snapshot = store.refresh(github)
    remaining, _ = apply_reconciliation(
        github,
        store,
        snapshot,
        github.open_issues(),
    )
    if remaining:
        raise RuntimeError("the issue was created but its Project state did not reconcile")
    print(url)
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    root, config, github, store = _context(args)
    snapshot = store.refresh(github)
    item = _issue(snapshot, args.issue)
    if item.get("state") != "OPEN":
        raise RuntimeError(f"issue #{args.issue} is not open")
    if item.get("status") != "Ready":
        raise RuntimeError(f"issue #{args.issue} is {item.get('status')!r}, not Ready")
    if item.get("active_claim"):
        raise RuntimeError(f"issue #{args.issue} already has an active claim")

    session = args.session or f"kanbanlan-{uuid.uuid4().hex[:8]}"
    branch, worktree = _claim_checkout(args, root, config, item)
    timestamp = _utc_timestamp()
    github.comment_issue(
        args.issue,
        (
            f"CLAIM: {timestamp}\n"
            f"Session: {session}\n"
            f"Branch: {branch}\n"
            f"Worktree: {worktree}\n"
            f"Touchpoints: {args.touchpoints}"
        ),
    )
    refreshed = store.refresh(github)
    claimed = _issue(refreshed, args.issue).get("active_claim") or {}
    if claimed.get("session") != session:
        github.comment_issue(
            args.issue,
            (f"RELEASED: {_utc_timestamp()} — concurrent claim lost\nSession: {session}"),
        )
        owner = claimed.get("session") or "another session"
        raise RuntimeError(f"issue #{args.issue} was claimed first by {owner}")

    _set_state(github, refreshed, args.issue, "status:in-progress", "In progress")
    try:
        if not args.no_worktree:
            _create_worktree(root, config, branch, Path(worktree))
    except Exception:
        github.comment_issue(
            args.issue,
            (f"RELEASED: {_utc_timestamp()} — worktree creation failed\nSession: {session}"),
        )
        latest = store.refresh(github)
        _set_state(github, latest, args.issue, "status:ready", "Ready")
        raise
    store.refresh(github)
    print(f"claimed #{args.issue} as {session}")
    print(f"branch: {branch}")
    print(f"worktree: {worktree}")
    return 0


def _claim_checkout(
    args: argparse.Namespace,
    root: Path,
    config: Config,
    item: dict[str, Any],
) -> tuple[str, str]:
    runner = Runner(root)
    if args.no_worktree:
        branch = args.branch or runner.run(["git", "branch", "--show-current"]).stdout.strip()
        if not branch or branch == config.default_branch:
            raise RuntimeError("--no-worktree requires an existing non-default branch or --branch")
        return branch, str(Path(args.worktree).resolve() if args.worktree else root)

    slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")[:48]
    branch = args.branch or f"work/{item['number']}-{slug or 'request'}"
    common = Path(
        runner.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
    ).resolve()
    default_worktree = common.parent / ".worktrees" / f"{item['number']}-{slug or 'request'}"
    worktree = Path(args.worktree).resolve() if args.worktree else default_worktree
    if worktree.exists():
        raise RuntimeError(f"worktree path already exists: {worktree}")
    return branch, str(worktree)


def _create_worktree(root: Path, config: Config, branch: str, worktree: Path) -> None:
    runner = Runner(root)
    runner.run(["git", "fetch", "origin", config.default_branch])
    branch_exists = (
        runner.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 0
    )
    args = ["git", "worktree", "add"]
    if branch_exists:
        args.extend([str(worktree), branch])
    else:
        args.extend(
            [
                "-b",
                branch,
                str(worktree),
                f"origin/{config.default_branch}",
            ]
        )
    runner.run(args)


def _cmd_release(args: argparse.Namespace) -> int:
    _, _, github, store = _context(args)
    snapshot = store.refresh(github)
    item = _issue(snapshot, args.issue)
    claim = item.get("active_claim")
    if not claim:
        raise RuntimeError(f"issue #{args.issue} has no active claim")
    session = claim.get("session") or "unknown"
    github.comment_issue(
        args.issue,
        (f"RELEASED: {_utc_timestamp()} — {args.reason}\nSession: {session}"),
    )
    latest = store.refresh(github)
    if args.blocked:
        _set_state(github, latest, args.issue, "status:blocked", "Blocked")
    else:
        _set_state(github, latest, args.issue, "status:ready", "Ready")
    store.refresh(github)
    print(f"released #{args.issue} from {session}")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    _, _, github, store = _context(args)
    snapshot = store.refresh(github)
    item = _issue(snapshot, args.issue)
    if not item.get("linked_open_pull_requests"):
        raise RuntimeError(f"issue #{args.issue} has no linked open pull request")
    _set_state(github, snapshot, args.issue, "status:review", "In review")
    store.refresh(github)
    print(f"moved #{args.issue} to In review")
    return 0


def _cmd_handoff(args: argparse.Namespace) -> int:
    _, _, github, store = _context(args)
    snapshot = store.refresh(github)
    item = _issue(snapshot, args.issue)
    if not item.get("active_claim"):
        raise RuntimeError(f"issue #{args.issue} has no active claim")
    github.comment_issue(
        args.issue,
        (
            f"HANDOFF: {_utc_timestamp()} — {args.reason}\n"
            f"Session: {args.session}\n"
            f"Branch: {args.branch}\n"
            f"Worktree: {Path(args.worktree).resolve()}"
        ),
    )
    refreshed = store.refresh(github)
    _set_state(github, refreshed, args.issue, "status:in-progress", "In progress")
    store.refresh(github)
    print(f"handed off #{args.issue} to {args.session}")
    return 0


def _set_state(
    github: GitHub,
    snapshot: dict[str, Any],
    issue_number: int,
    label: str,
    status: str,
) -> None:
    item = _issue(snapshot, issue_number)
    github.set_issue_status_label(issue_number, label)
    github.set_project_status(
        item["project_item_id"],
        snapshot["project"],
        status,
    )


def _issue(snapshot: dict[str, Any], number: int) -> dict[str, Any]:
    item = next(
        (
            value
            for value in snapshot.get("items", [])
            if value.get("type") == "ISSUE" and value.get("number") == number
        ),
        None,
    )
    if not item:
        raise RuntimeError(f"issue #{number} is not on the configured Project")
    return item


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
