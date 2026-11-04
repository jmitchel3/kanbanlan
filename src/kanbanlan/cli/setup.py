"""Setup commands: init, auth, upgrade, doctor, account, and worker removal."""

from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kanbanlan.accounts import (
    AccountStore,
    logged_in_accounts,
    owner_key,
    repository_key,
    resolve_account,
)
from kanbanlan.cli.context import _context, _root
from kanbanlan.cli.output import _emit_result, _rate_limit_text
from kanbanlan.config import (
    CONFIG_FILENAME,
    Config,
    cache_dir,
    discover_default_branch,
    discover_repository,
)
from kanbanlan.github import REQUIRED_STATUS_OPTIONS, GitHub
from kanbanlan.legacy import pending_legacy_changes, uninstall_worker, worker_leftovers
from kanbanlan.runner import CommandError, Runner
from kanbanlan.scaffold import PRIORITY_LABELS, STATUS_LABELS, scaffold_repository
from kanbanlan.snapshot import CacheStore
from kanbanlan.ui import (
    BOLD,
    CYAN,
    DIM,
    field,
    heading,
    section,
    status,
    status_value,
    style,
    success,
    warning,
)
from kanbanlan.workflow import apply_reconciliation, plan_reconciliation

PROJECT_URL_RE = re.compile(r"github\.com/(?:orgs|users)/(?P<owner>[^/]+)/projects/(?P<number>\d+)")


DEFAULT_TEMPLATE_OWNER = "jmitchel3"


DEFAULT_TEMPLATE_NUMBER = 6


DEFAULT_TEMPLATE_URL = "https://github.com/users/jmitchel3/projects/6/views/5"


@dataclass(frozen=True)
class ProjectChoice:
    mode: str
    number: int | None = None
    template_owner: str | None = None
    template_number: int | None = None
    title: str | None = None


@dataclass(frozen=True)
class InitPlan:
    repository: str
    project_owner: str
    project_owner_type: str
    project: ProjectChoice
    default_branch: str
    stage_branch: str
    production_branch: str
    hostname: str
    stale_seconds: int
    session_tracking: bool
    reconcile: bool
    open_project: bool
    local_only: bool


@dataclass(frozen=True)
class PromptChoice:
    key: str
    label: str
    detail: str = ""


def _cmd_init(args: argparse.Namespace) -> int:
    root = _root(args)
    if (root / CONFIG_FILENAME).exists() and not args.reconfigure:
        return _cmd_init_update(args, root)

    hostname = args.hostname or "github.com"
    stale_seconds = args.stale_seconds if args.stale_seconds is not None else 180
    repository = args.repository or discover_repository(root)
    if repository.count("/") != 1 or any(not part for part in repository.split("/")):
        raise RuntimeError("--repository must use OWNER/NAME")
    if stale_seconds < 1:
        raise RuntimeError("--stale-seconds must be positive")
    repo_owner, repo_name = repository.split("/", 1)
    detected_default_branch = args.default_branch or discover_default_branch(root)
    github = GitHub(root)
    interactive = not args.non_interactive

    if interactive:
        heading("Kanbanlan setup")
        print("Configure a shared GitHub Project workflow in three short steps.")
        section("1 of 3 · Repository")
        field("Repository", repository)
        field("Root", root)

    if args.local_only:
        project_owner, project_number = _project_reference(args, repo_owner)
        if project_number is None:
            raise RuntimeError("--local-only requires --project-number or --project-url")
        owner_type = args.owner_type or "organization"
        choice = ProjectChoice(mode="existing", number=project_number)
    else:
        github.ensure_auth(hostname, interactive=interactive)
        with status("Reading GitHub repository settings"):
            repository_info = github.repository_info(repository)
        detected_default_branch = (
            args.default_branch
            or (repository_info.get("defaultBranchRef") or {}).get("name")
            or detected_default_branch
        )
        project_owner, project_number = _project_reference(args, repo_owner)
        project_owner = project_owner or repository_info["owner"]["login"]
        if interactive and not args.project_owner and not args.project_url:
            project_owner = _prompt_text("Project owner", default=project_owner)
        if args.owner_type:
            owner_type = args.owner_type
        else:
            with status(f"Checking Project owner {project_owner}"):
                owner_type = github.detect_owner_type(project_owner)
        github.ensure_project_scope(
            project_owner,
            hostname,
            owner_type=owner_type,
            interactive=interactive,
        )
        choice = _choose_project(
            args,
            github,
            project_owner,
            repo_name,
            project_number,
        )

    default_branch = detected_default_branch
    stage_branch = args.stage_branch or default_branch
    production_branch = args.production_branch or ""
    open_project = bool(args.open)
    if interactive:
        section("3 of 3 · Delivery branches")
        print("These branches define where work is reviewed and delivered.")
        if args.default_branch is None:
            default_branch = _prompt_text("Pull request target branch", default=default_branch)
        if args.stage_branch is None:
            stage_branch = _prompt_text("Staging branch", default=default_branch)
        if args.production_branch is None:
            production_branch = _prompt_text(
                "Production branch (optional)",
                default="",
                required=False,
            )
        if args.open is None and not args.local_only:
            open_project = _prompt_bool("Open the Project in a browser after setup?", default=False)

    plan = InitPlan(
        repository=repository,
        project_owner=project_owner,
        project_owner_type=owner_type,
        project=choice,
        default_branch=default_branch,
        stage_branch=stage_branch,
        production_branch=production_branch,
        hostname=hostname,
        stale_seconds=stale_seconds,
        session_tracking=bool(args.session_tracking),
        reconcile=not args.skip_reconcile and not args.local_only,
        open_project=open_project and not args.local_only,
        local_only=args.local_only,
    )
    if interactive:
        _print_init_summary(plan)
        if not _prompt_bool("Apply this setup?", default=True):
            warning("Setup cancelled; no repository files or Project settings were changed.")
            return 0

    if interactive:
        section("Applying setup")
    materialize_label = {
        "existing": "Using selected GitHub Project",
        "create": "Creating GitHub Project",
        "copy": "Copying template GitHub Project",
    }[plan.project.mode]
    with status(materialize_label):
        project_number = _materialize_project(github, plan.project, project_owner)
    config = Config(
        repository=plan.repository,
        project_owner=plan.project_owner,
        project_owner_type=plan.project_owner_type,
        project_number=project_number,
        default_branch=plan.default_branch,
        stage_branch=plan.stage_branch,
        production_branch=plan.production_branch,
        hostname=plan.hostname,
        stale_seconds=plan.stale_seconds,
        session_tracking=plan.session_tracking,
    )
    with status("Writing repository configuration"):
        results = scaffold_repository(root, config, force=args.force)
    _print_scaffold_results(results, root)

    if plan.local_only:
        success("Local setup complete.")
        print("Run 'kanbanlan doctor' when GitHub access is available.")
        return 0

    github.config = config
    with status("Linking Project to repository"):
        github.link_project()
    with status("Checking Project Status options"):
        changed = github.ensure_status_options()
    project_status_result = (
        "  updated    Project Status options" if changed else "  unchanged  Project Status options"
    )
    print(project_status_result)
    with status("Creating workflow labels"):
        github.ensure_labels()

    store = CacheStore(config, cache_dir(root))
    with status("Refreshing the shared board snapshot"):
        snapshot = store.refresh(github, projection=True)
    if plan.reconcile:
        with status("Checking existing issue and Project state"):
            open_issues = github.open_issues()
            drift = plan_reconciliation(snapshot, open_issues)
        if drift:
            print(f"reconciling {len(drift)} existing issue/Project differences")
            with status(f"Repairing {len(drift)} board difference(s)"):
                remaining, _ = apply_reconciliation(
                    github,
                    store,
                    snapshot,
                    open_issues,
                )
            if remaining:
                raise RuntimeError(
                    f"{len(remaining)} reconciliation differences remain after setup"
                )
    if plan.open_project:
        github.open_project()
    success(f"Kanbanlan configured for {repository} and Project {project_owner}/{project_number}.")
    print("Next: run 'kanbanlan doctor' to verify the setup, then 'kanbanlan next'.")
    return 0


def _cmd_init_update(args: argparse.Namespace, root: Path) -> int:
    reconfiguration_options = [
        option
        for option, selected in (
            ("--repository", args.repository is not None),
            ("--project-owner", args.project_owner is not None),
            ("--project-url", args.project_url is not None),
            ("--project-number", args.project_number is not None),
            ("--owner-type", args.owner_type is not None),
            ("--create-project", args.create_project),
            ("--template-project", args.template_project is not None),
            ("--project-title", args.project_title is not None),
        )
        if selected
    ]
    if reconfiguration_options:
        supplied = ", ".join(reconfiguration_options)
        raise RuntimeError(
            f"{supplied} would change the existing repository or Project binding; "
            "rerun with --reconfigure to start full setup explicitly"
        )
    if args.stale_seconds is not None and args.stale_seconds < 1:
        raise RuntimeError("--stale-seconds must be positive")

    existing = Config.load(root)
    updated = replace(
        existing,
        default_branch=args.default_branch or existing.default_branch,
        stage_branch=args.stage_branch or existing.stage_branch,
        production_branch=(
            args.production_branch
            if args.production_branch is not None
            else existing.production_branch
        ),
        hostname=args.hostname or existing.hostname,
        stale_seconds=(
            args.stale_seconds if args.stale_seconds is not None else existing.stale_seconds
        ),
        session_tracking=(
            args.session_tracking
            if args.session_tracking is not None
            else existing.session_tracking
        ),
    )
    interactive = not args.non_interactive
    changed = existing != updated
    if interactive:
        heading("Kanbanlan update")
        print(
            "Existing configuration detected. The repository and GitHub Project binding "
            "will be reused."
        )
        section("Configuration")
        field("Repository", updated.repository)
        field("Project", f"{updated.project_owner}/{updated.project_number} (reused)")
        field("Pull request target", updated.default_branch)
        field("Staging branch", updated.stage_branch)
        field("Production branch", updated.production_branch or "not configured")
        field("Snapshot freshness", f"{updated.stale_seconds} seconds")
        field("Agent session tracking", "enabled" if updated.session_tracking else "disabled")
        if not changed:
            print("No configuration overrides were supplied; managed files will be refreshed.")
        if not _prompt_bool("Apply this in-place update?", default=True):
            warning("Update cancelled; no repository files or Project settings were changed.")
            return 0

    with status("Updating existing repository configuration"):
        results = scaffold_repository(root, updated, force=args.force)
    _print_scaffold_results(results, root)
    if args.open and not args.local_only:
        GitHub(root, updated).open_project()
    action = "updated" if changed else "refreshed"
    success(
        f"Kanbanlan configuration {action} in place; "
        f"Project {updated.project_owner}/{updated.project_number} was reused."
    )
    if not updated.session_tracking:
        print("Existing generated session hooks, if any, remain installed but inactive.")
    return 0


def _print_scaffold_results(results: list[Any], root: Path) -> None:
    for result in results:
        marker = style(f"{result.action:<10}", CYAN if result.action != "unchanged" else DIM)
        print(f"  {marker} {result.path.relative_to(root)}")


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
    if number is not None and number < 1:
        raise RuntimeError("Project number must be positive")
    return owner, number


def _choose_project(
    args: argparse.Namespace,
    github: GitHub,
    owner: str,
    repo_name: str,
    project_number: int | None,
) -> ProjectChoice:
    title = args.project_title or f"{repo_name} Delivery"
    if project_number:
        return ProjectChoice(mode="existing", number=project_number)
    if args.template_project:
        source_owner, source_number = _parse_template(args.template_project)
        if not args.non_interactive and args.project_title is None:
            title = _prompt_text("New Project title", default=title)
        return ProjectChoice(
            mode="copy",
            template_owner=source_owner,
            template_number=source_number,
            title=title,
        )
    if args.create_project:
        if not args.non_interactive and args.project_title is None:
            title = _prompt_text("New Project title", default=title)
        return ProjectChoice(mode="create", title=title)

    if args.non_interactive:
        return ProjectChoice(
            mode="copy",
            template_owner=DEFAULT_TEMPLATE_OWNER,
            template_number=DEFAULT_TEMPLATE_NUMBER,
            title=title,
        )

    with status(f"Loading Projects owned by {owner}"):
        projects = github.list_projects(owner)

    section("2 of 3 · GitHub Project")
    options = [
        PromptChoice(
            "default",
            "Create a preconfigured Project",
            f"fresh copy of {DEFAULT_TEMPLATE_URL}",
        ),
        *[
            PromptChoice(
                key=f"project:{project['number']}",
                label=str(project["title"]),
                detail=f"existing Project #{project['number']}",
            )
            for project in projects
        ],
    ]
    options.extend(
        [
            PromptChoice("new", "Create an empty Project", "no preconfigured views"),
            PromptChoice("copy", "Copy another template Project", "includes template views"),
        ]
    )
    selected = _prompt_choice("Choose a Project", options)
    if selected.key == "default":
        if args.project_title is None:
            title = _prompt_text("New Project title", default=title)
        return ProjectChoice(
            mode="copy",
            template_owner=DEFAULT_TEMPLATE_OWNER,
            template_number=DEFAULT_TEMPLATE_NUMBER,
            title=title,
        )
    if selected.key == "new":
        title = _prompt_text("New Project title", default=title)
        return ProjectChoice(mode="create", title=title)
    if selected.key == "copy":
        while True:
            try:
                source_owner, source_number = _parse_template(
                    _prompt_text("Template Project (OWNER/NUMBER)")
                )
                break
            except RuntimeError as exc:
                warning(str(exc))
        title = _prompt_text("New Project title", default=title)
        return ProjectChoice(
            mode="copy",
            template_owner=source_owner,
            template_number=source_number,
            title=title,
        )
    return ProjectChoice(mode="existing", number=int(selected.key.removeprefix("project:")))


def _materialize_project(github: GitHub, choice: ProjectChoice, owner: str) -> int:
    if choice.mode == "existing" and choice.number:
        return choice.number
    if choice.mode == "create" and choice.title:
        return _project_number(github.create_project(owner, choice.title))
    if choice.mode == "copy" and choice.template_owner and choice.template_number and choice.title:
        created = github.copy_project(
            choice.template_owner,
            choice.template_number,
            owner,
            choice.title,
        )
        return _project_number(created)
    raise RuntimeError("invalid Project setup choice")


def _prompt_text(
    label: str,
    *,
    default: str | None = None,
    required: bool = True,
) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            response = input(f"{style(label, BOLD)}{style(suffix, DIM)}: ").strip()
        except EOFError as exc:
            raise RuntimeError(
                "interactive setup requires terminal input; rerun with --non-interactive"
            ) from exc
        value = response if response else default
        if value is not None and (value or not required):
            return value
        warning(f"{label} is required.")


def _prompt_choice(
    label: str,
    choices: list[PromptChoice],
    *,
    default: int = 0,
) -> PromptChoice:
    if not choices:
        raise RuntimeError("no choices are available")
    if default < 0 or default >= len(choices):
        raise ValueError("default choice is out of range")

    print(f"{label}:")
    for index, choice in enumerate(choices, start=1):
        marker = style(f"[{index}]", CYAN, BOLD)
        detail = f" — {style(choice.detail, DIM)}" if choice.detail else ""
        print(f"  {marker} {choice.label}{detail}")

    by_key = {choice.key.casefold(): choice for choice in choices}
    while True:
        response = _prompt_text("Selection", default=str(default + 1)).casefold()
        if response in by_key:
            return by_key[response]
        try:
            selected = int(response)
        except ValueError:
            selected = 0
        if 1 <= selected <= len(choices):
            return choices[selected - 1]
        warning(f"Choose a number from 1 to {len(choices)}.")


def _prompt_bool(label: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        try:
            response = input(f"{label} [{suffix}]: ").strip().lower()
        except EOFError as exc:
            raise RuntimeError(
                "interactive setup requires terminal input; rerun with --non-interactive"
            ) from exc
        if not response:
            return default
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        warning("Please answer yes or no.")


def _print_init_summary(plan: InitPlan) -> None:
    project = plan.project
    if project.mode == "existing":
        project_summary = f"use existing Project #{project.number}"
    elif project.mode == "create":
        project_summary = f"create {project.title!r}"
    else:
        project_summary = (
            f"copy {project.template_owner}/{project.template_number} as {project.title!r}"
        )
    section("Review setup")
    field("Repository", plan.repository)
    field("Project", f"{plan.project_owner} ({plan.project_owner_type}); {project_summary}")
    field("Pull request target", plan.default_branch)
    field("Staging branch", plan.stage_branch)
    field("Production branch", plan.production_branch or "not configured")
    field("Agent session tracking", "enabled" if plan.session_tracking else "disabled")
    field("Reconcile open issues", "yes" if plan.reconcile else "no")
    if not plan.local_only:
        field("Open Project after setup", "yes" if plan.open_project else "no")


def _parse_template(value: str) -> tuple[str, int]:
    try:
        owner, raw_number = value.rsplit("/", 1)
        number = int(raw_number)
    except ValueError as exc:
        raise RuntimeError("--template-project must use OWNER/NUMBER") from exc
    if not owner or number < 1:
        raise RuntimeError("--template-project must use OWNER/POSITIVE_NUMBER")
    return owner, number


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
    github.ensure_project_scope(
        config.project_owner,
        config.hostname,
        owner_type=config.project_owner_type,
        interactive=True,
    )
    success("GitHub authentication and Project scope are ready.")
    return 0


def _cmd_upgrade(args: argparse.Namespace) -> int:
    if shutil.which("uv") is None:
        raise RuntimeError(
            "uv is required to upgrade Kanbanlan; install it from https://docs.astral.sh/uv/"
        )
    try:
        with status("Upgrading Kanbanlan"):
            Runner().run(
                ["uv", "tool", "upgrade", "kanbanlan"],
                capture=False,
                timeout=None,
            )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "uv is required to upgrade Kanbanlan; install it from https://docs.astral.sh/uv/"
        ) from exc
    success("Kanbanlan upgrade complete.")
    print("Run 'kanbanlan --version' to verify the installed version.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    root, config, github, store = _context(args)
    failures: list[str] = []
    heading("Kanbanlan doctor")
    field("Config", root / ".kanbanlan.toml")
    field("Repository", config.repository)
    field("Project", f"{config.project_owner}/{config.project_number}")

    with status("Checking GitHub authentication"):
        auth = github.runner.run(
            ["gh", "auth", "status", "--active", "--hostname", config.hostname],
            check=False,
        )
    if auth.returncode:
        failures.append("GitHub authentication is unavailable; run 'kanbanlan auth'")
    else:
        success("GitHub authentication")
        try:
            with status("Checking Project Status field"):
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
                success("Project Status field")
            with status("Checking repository workflow labels"):
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
                success("Repository workflow labels")
        except (CommandError, RuntimeError) as exc:
            failures.append(str(exc))
    inspection = store.inspect()
    field("Cache", f"{status_value(inspection['snapshot_state'])} ({store.snapshot_path})")
    rate = inspection.get("rate_limit") or {}
    if rate.get("remaining") is not None:
        field("Rate limit", _rate_limit_text(rate))
        if rate.get("resource") != "core" and rate["remaining"] < config.rate_limit_floor:
            warning(
                f"GraphQL points are below the configured floor of {config.rate_limit_floor}; "
                "snapshot refreshes are deferred until the quota resets"
            )
    # Leftovers of the removed worker and outbox warn without failing the check.
    leftovers = worker_leftovers()
    for unit in leftovers["units"]:
        warning(
            f"the removed background worker is still installed as {unit}; "
            "run 'kanbanlan admin worker uninstall'"
        )
    if leftovers["pid"] is not None:
        warning(
            f"the removed background worker is still running (pid {leftovers['pid']}); "
            "run 'kanbanlan admin worker uninstall'"
        )
    queued = pending_legacy_changes(store.directory)
    if queued:
        warning(
            f"{queued} change(s) an older Kanbanlan queued for GitHub are still waiting; "
            "the next lifecycle command applies them or reports how to retry them"
        )
    if failures:
        for failure in failures:
            warning(failure)
        print("Run 'kanbanlan auth' for access problems or 'kanbanlan init' to repair setup.")
        return 1
    success("All checks passed")
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    """Remove what the background worker of an older version left installed."""

    payload = uninstall_worker()
    if _emit_result(args, payload):
        return 0
    for unit in payload["units_removed"]:
        success(f"Removed worker unit {unit}")
    if payload["stopped_pid"] is not None:
        success(f"Stopped worker process {payload['stopped_pid']}")
    for path in payload["state_removed"]:
        success(f"Removed {path}")
    if not any(payload.values()):
        print("No background worker is installed.")
    return 0


def _cmd_account(args: argparse.Namespace) -> int:
    root = _root(args)
    config = Config.load(root)
    store = AccountStore()
    owner = config.repository.split("/", 1)[0]
    key = owner_key(owner) if args.owner else repository_key(config.repository)
    if args.action == "use":
        if not args.login:
            raise RuntimeError("account use requires a LOGIN")
        known = logged_in_accounts(config.hostname)
        if args.login.lower() not in {value.lower() for value in known}:
            raise RuntimeError(
                f"{args.login} is not logged in to {config.hostname} in gh "
                f"(logged in: {', '.join(sorted(known)) or 'none'}); "
                f"run 'gh auth login --hostname {config.hostname}' first"
            )
        store.bind(config.hostname, key, args.login)
    elif args.action == "clear":
        if args.login:
            raise RuntimeError("account clear takes no LOGIN")
        store.unbind(config.hostname, key)
    elif args.login:
        raise RuntimeError("account show takes no LOGIN")
    account = resolve_account(config, store=store, persist=False) if args.action != "use" else None
    if args.action == "use":
        account = store.lookup(config.hostname, config.repository)
    result = {
        "repository": config.repository,
        "hostname": config.hostname,
        "login": account.login if account else None,
        "source": account.source if account else "environment token",
    }
    if _emit_result(args, result):
        return 0
    field("Repository", config.repository)
    field("Account", result["login"] or "(GH_TOKEN from the environment)")
    field("Source", result["source"])
    return 0
