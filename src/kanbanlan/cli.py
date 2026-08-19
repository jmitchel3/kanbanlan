from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kanbanlan import __version__
from kanbanlan.config import (
    CONFIG_FILENAME,
    Config,
    cache_dir,
    common_dir,
    discover_default_branch,
    discover_repository,
    find_repo_root,
    primary_worktree,
)
from kanbanlan.domain import request_label, resolve_request_item
from kanbanlan.github import REQUIRED_STATUS_OPTIONS, GitHub
from kanbanlan.identity import attach_kanbanlan_id, new_kanbanlan_id
from kanbanlan.providers import CoordinationProvider, create_provider
from kanbanlan.records import create_record
from kanbanlan.registry import RegistryStore
from kanbanlan.runner import CommandError, Runner, is_transient_failure
from kanbanlan.scaffold import PRIORITY_LABELS, STATUS_LABELS, scaffold_repository
from kanbanlan.sessions import (
    AgentSession,
    SessionContextStore,
    activity_comment,
    detect_agent_session,
    hook_workspaces,
    session_from_hook_payload,
)
from kanbanlan.snapshot import SCOPE_PROJECT, CacheStore, utc_now
from kanbanlan.ui import (
    BOLD,
    CYAN,
    DIM,
    configure_color,
    configure_progress,
    error,
    field,
    heading,
    priority_value,
    section,
    status,
    status_value,
    style,
    success,
    warning,
)
from kanbanlan.updates import notify_if_update_available
from kanbanlan.worker import Worker, start_worker, stop_worker, worker_status
from kanbanlan.workflow import (
    apply_reconciliation,
    format_drift,
    plan_reconciliation,
)

PROJECT_URL_RE = re.compile(r"github\.com/(?:orgs|users)/(?P<owner>[^/]+)/projects/(?P<number>\d+)")
DEFAULT_TEMPLATE_OWNER = "jmitchel3"
DEFAULT_TEMPLATE_NUMBER = 6
DEFAULT_TEMPLATE_URL = "https://github.com/users/jmitchel3/projects/6/views/5"
COMMAND_NAMES = (
    "init",
    "auth",
    "upgrade",
    "doctor",
    "ensure",
    "refresh",
    "status",
    "snapshot",
    "path",
    "next",
    "reconcile",
    "capture",
    "triage",
    "claim",
    "release",
    "review",
    "handoff",
    "sessions",
    "resume",
    "session-hook",
    "record",
    "worker",
)


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


class KanbanlanParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        hint = f"Run '{self.prog} --help' to see available options."
        invalid_command = re.search(r"invalid choice: '([^']+)'", message)
        if invalid_command:
            suggestions = difflib.get_close_matches(invalid_command.group(1), COMMAND_NAMES, n=1)
            if suggestions:
                hint = (
                    f"Did you mean '{suggestions[0]}'? Run '{self.prog} --help' for all commands."
                )
        error(message, hint=hint)
        raise SystemExit(2)


def _add_project_scope_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--project",
        dest="project_scope",
        action="store_true",
        help="read every repository on the configured Project instead of this one",
    )


def _add_actor_session_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--actor-session",
        metavar="HARNESS:SESSION_ID",
        help="provider-native session that performed this lifecycle action",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = KanbanlanParser(
        prog="kanbanlan",
        description="Repository-native request coordination for humans and coding agents.",
        epilog=(
            "Common workflows:\n"
            "  kanbanlan init                 Configure this repository\n"
            "  kanbanlan doctor               Diagnose configuration and access\n"
            "  kanbanlan next                 Find the next Ready request\n"
            "  kanbanlan reconcile --apply    Repair board drift"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-C",
        "--repo-root",
        help="run against this Git repository instead of the current directory",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="terminal colors (default: auto; NO_COLOR is also respected)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit stable JSON for agents and automation",
    )
    parser.add_argument("--version", action="version", version=f"kanbanlan {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser(
        "init",
        help="configure a repository and GitHub Project",
        description=(
            "Update an existing Kanbanlan configuration in place, or interactively configure "
            "a new repository and GitHub Project workflow."
        ),
        epilog=(
            "Examples:\n"
            "  kanbanlan init  # set up a new repo or refresh an existing one\n"
            "  kanbanlan init --session-tracking  # update an existing repository in place\n"
            "  kanbanlan init --no-session-tracking\n"
            "  kanbanlan init --reconfigure  # intentionally rerun full Project setup\n"
            "  kanbanlan init --project-url https://github.com/orgs/acme/projects/2\n"
            "  kanbanlan init --create-project --project-title 'Product Delivery' --open\n"
            "  kanbanlan init --project-number 2 --local-only"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    init.add_argument("--repository", help="GitHub owner/repository; defaults from origin")
    init.add_argument("--project-owner", help="Project owner; defaults to repository owner")
    project_source = init.add_mutually_exclusive_group()
    project_source.add_argument("--project-url", help="existing GitHub Project URL")
    project_source.add_argument("--project-number", type=int, help="existing Project number")
    init.add_argument(
        "--owner-type",
        choices=("organization", "user"),
        help="Project owner type; normally auto-detected",
    )
    project_source.add_argument(
        "--create-project", action="store_true", help="create a new empty Project"
    )
    project_source.add_argument(
        "--template-project",
        metavar="OWNER/NUMBER",
        help="copy this Project instead of the default Kanbanlan template",
    )
    init.add_argument("--project-title", help="title for a newly created/copied Project")
    init.add_argument("--default-branch", help="delivery branch; defaults from origin")
    init.add_argument("--stage-branch", help="branch deployed to staging")
    init.add_argument("--production-branch", help="optional production branch")
    init.add_argument("--hostname", help="GitHub hostname; preserved for existing configuration")
    init.add_argument(
        "--stale-seconds",
        type=int,
        help="snapshot freshness window; defaults to 180 for new configuration",
    )
    init.add_argument(
        "--session-tracking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable provider-native agent session attribution",
    )
    init.add_argument(
        "--reconfigure",
        action="store_true",
        help="run full setup even when this repository already has configuration",
    )
    init.add_argument("--force", action="store_true", help="replace custom generated targets")
    init.add_argument(
        "--non-interactive",
        action="store_true",
        help="disable prompts and use defaults for omitted optional choices",
    )
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
    init.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="open the Project in a browser after setup",
    )

    commands.add_parser("auth", help="repair GitHub login and Project scope")
    commands.add_parser("upgrade", help="upgrade Kanbanlan to the latest release")
    commands.add_parser("doctor", help="check local config, auth, fields, and labels")
    commands.add_parser("ensure", help="ensure the shared snapshot is fresh")
    commands.add_parser("refresh", help="refresh the shared snapshot now")
    status_command = commands.add_parser("status", help="show local cache and board summary")
    _add_project_scope_argument(status_command)
    snapshot_command = commands.add_parser("snapshot", help="print the current snapshot JSON")
    _add_project_scope_argument(snapshot_command)
    commands.add_parser("path", help="print the shared snapshot path")
    commands.add_parser("next", help="show the first unblocked Ready card")
    commands.add_parser(
        "overlap",
        help="list open requests and pull requests across the whole Project",
    )

    reconcile = commands.add_parser("reconcile", help="report label/claim/PR/Project drift")
    reconcile.add_argument("--apply", action="store_true", help="apply the displayed repairs")

    capture = commands.add_parser("capture", help="create an Inbox request card")
    capture.add_argument("title", help="issue title")
    capture.add_argument("--body", default="", help="issue body; defaults to an outcome template")
    capture.add_argument(
        "--priority",
        choices=tuple(PRIORITY_LABELS),
        default="priority:p2",
        help="request priority (default: priority:p2)",
    )
    _add_actor_session_argument(capture)

    triage = commands.add_parser("triage", help="move one Inbox request to Ready")
    triage.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    _add_actor_session_argument(triage)

    claim = commands.add_parser("claim", help="claim one Ready issue")
    claim.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    claim.add_argument("--touchpoints", required=True, help="expected files or systems to change")
    claim.add_argument("--session", help="claim owner; generated when omitted")
    claim.add_argument("--branch", help="work branch; generated when omitted")
    claim.add_argument("--worktree", help="worktree path; generated when omitted")
    claim.add_argument(
        "--no-worktree",
        action="store_true",
        help="claim using the current branch/worktree instead of creating one",
    )
    _add_actor_session_argument(claim)

    release = commands.add_parser("release", help="release an active claim")
    release.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    release.add_argument("--reason", required=True, help="why the claim is being released")
    release.add_argument("--blocked", action="store_true", help="move to Blocked instead of Ready")
    _add_actor_session_argument(release)

    review = commands.add_parser("review", help="move an issue with an open PR to review")
    review.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    _add_actor_session_argument(review)

    handoff = commands.add_parser("handoff", help="transfer an active claim")
    handoff.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    handoff.add_argument("--session", required=True, help="new owner/session identifier")
    handoff.add_argument("--branch", required=True, help="branch the new owner should continue")
    handoff.add_argument("--worktree", required=True, help="worktree the new owner should continue")
    handoff.add_argument("--reason", required=True, help="handoff context")
    _add_actor_session_argument(handoff)

    sessions = commands.add_parser(
        "sessions",
        help="show agent sessions that performed lifecycle activity on one request",
    )
    sessions.add_argument("request", help="Kanbanlan ID or canonical provider reference")
    sessions.add_argument("--action", help="only show one lifecycle action")

    resume = commands.add_parser(
        "resume",
        help="print or run the latest recorded agent resume command for one request",
    )
    resume.add_argument("request", help="Kanbanlan ID or canonical provider reference")
    resume.add_argument("--action", help="select the latest session for one lifecycle action")
    resume.add_argument(
        "--run",
        action="store_true",
        help="replace Kanbanlan with the native agent resume command",
    )

    session_hook = commands.add_parser(
        "session-hook",
        help="register provider-native session context from an agent lifecycle hook",
    )
    session_hook.add_argument(
        "--agent",
        required=True,
        help="agent harness supplying the hook payload (codex, claude, grok, or agy)",
    )

    record = commands.add_parser(
        "record",
        help="create a durable repository record for one request",
    )
    record.add_argument("request", help="Kanbanlan ID or canonical provider reference")

    worker = commands.add_parser("worker", help="manage the user-scoped background reconciler")
    worker.add_argument(
        "action",
        choices=("status", "enable", "disable", "start", "stop", "run"),
        help="worker lifecycle action",
    )
    worker.add_argument(
        "--github-login",
        help="GitHub account to use for this repository without switching the active account",
    )
    worker.add_argument(
        "--interval",
        type=int,
        default=300,
        help="worker polling interval in seconds (default: 300)",
    )
    worker.add_argument("--once", action="store_true", help="run one worker iteration and exit")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_color(args.color)
    configure_progress(not args.json_output)
    if args.command not in {"upgrade", "session-hook"} and not args.json_output:
        notify_if_update_available(__version__)
    try:
        handler = globals()[f"_cmd_{args.command.replace('-', '_')}"]
        return int(handler(args) or 0)
    except KeyboardInterrupt:
        _emit_error(args, "cancelled", kind="KeyboardInterrupt")
        return 130
    except FileNotFoundError as exc:
        command = exc.filename or "required command"
        _emit_error(
            args,
            f"{command!r} was not found",
            kind=exc.__class__.__name__,
            hint=_missing_command_hint(command),
        )
        return 1
    except (CommandError, RuntimeError, ValueError) as exc:
        message, hint = _friendly_error(exc)
        _emit_error(args, message, kind=exc.__class__.__name__, hint=hint)
        return 1


def _write_json(value: dict[str, Any], *, stream: Any = None) -> None:
    # Resolve the stream on each call so a redirected stdout is honored.
    target = stream if stream is not None else sys.stdout
    json.dump(value, target, indent=2, sort_keys=True)
    target.write("\n")


def _emit_result(args: argparse.Namespace, value: dict[str, Any]) -> bool:
    if not args.json_output:
        return False
    _write_json({"ok": True, "result": value})
    return True


def _emit_error(
    args: argparse.Namespace,
    message: str,
    *,
    kind: str,
    hint: str | None = None,
) -> None:
    if args.json_output:
        _write_json(
            {
                "ok": False,
                "error": {
                    "kind": kind,
                    "message": message,
                    "hint": hint,
                },
            },
            stream=sys.stderr,
        )
        return
    error(message, hint=hint)


def _missing_command_hint(command: str) -> str:
    name = Path(command).name
    if name == "gh":
        return "Install GitHub CLI from https://cli.github.com, then run 'kanbanlan auth'."
    if name == "git":
        return "Install Git and rerun the command from a Git repository."
    if name == "uv":
        return "Install uv from https://docs.astral.sh/uv/."
    return f"Install {name!r} and make sure it is available on PATH."


def _friendly_error(exc: Exception) -> tuple[str, str | None]:
    if not isinstance(exc, CommandError):
        message = str(exc)
        hint = None
        if ".kanbanlan.toml" in message and "missing" in message:
            hint = "Run 'kanbanlan init' from the repository root."
        elif "not a git repository" in message.lower():
            hint = "Run inside a Git repository or pass '-C /path/to/repository'."
        return message, hint

    result = exc.result
    command = shlex.join(result.args)
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    normalized = detail.lower()
    hint = f"Run `{command}` directly for more detail."
    if result.args and result.args[0] == "gh":
        if is_transient_failure(result):
            hint = (
                "GitHub returned a temporary server error and retries did not clear it. "
                "Check https://www.githubstatus.com, then run this command again."
            )
        elif "unknown owner type" in normalized:
            hint = (
                "The GitHub CLI reports this whenever it cannot resolve the owner, "
                "including during a GitHub outage. Confirm the owner name, check "
                "https://www.githubstatus.com, then run this command again."
            )
        elif any(value in normalized for value in ("auth", "credential", "401", "scope")):
            hint = "Run 'kanbanlan auth' to repair GitHub login and Project access."
        elif any(
            value in normalized for value in ("connect", "network", "resolve host", "timed out")
        ):
            hint = "Check network access to GitHub, then retry this command."
    elif result.args[:3] == ("git", "rev-parse", "--show-toplevel"):
        return (
            "the selected directory is not inside a Git repository",
            "Run inside a Git repository or pass '-C /path/to/repository'.",
        )
    return f"command failed ({result.returncode}): {command}\n{detail}", hint


def _root(args: argparse.Namespace) -> Path:
    start = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    return find_repo_root(start)


def _context(
    args: argparse.Namespace,
) -> tuple[Path, Config, CoordinationProvider, CacheStore]:
    root = _root(args)
    config = Config.load(root)
    provider = create_provider(root, config)
    store = CacheStore(config, cache_dir(root))
    return root, config, provider, store


def _actor_session(
    args: argparse.Namespace,
    root: Path,
    config: Config,
) -> AgentSession | None:
    explicit = getattr(args, "actor_session", None)
    if not config.session_tracking_enabled():
        if explicit:
            raise RuntimeError(
                "--actor-session requires session tracking; set "
                "[session_tracking].enabled = true or KANBANLAN_SESSION_TRACKING=true"
            )
        return None
    return detect_agent_session(
        explicit=explicit,
        store=SessionContextStore(cache_dir(root)),
        root=root,
    )


def _record_session_activity(
    *,
    config: Config,
    provider: CoordinationProvider,
    reference: int | str,
    action: str,
    from_status: str | None,
    to_status: str | None,
    actor: AgentSession | None,
    owner_session: str | None = None,
) -> None:
    if not config.session_tracking_enabled():
        return
    provider.comment_request(
        reference,
        activity_comment(
            action=action,
            at=_utc_timestamp(),
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            owner_session=owner_session,
        ),
    )


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
        snapshot = store.refresh(github)
    reconciled_successfully = False
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
        reconciled_successfully = True
    if plan.open_project:
        github.open_project()
    if reconciled_successfully:
        _activate_worker(root, config)
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
    cache_state = store.inspect()["snapshot_state"]
    field("Cache", f"{status_value(cache_state)} ({store.snapshot_path})")
    if failures:
        for failure in failures:
            warning(failure)
        print("Run 'kanbanlan auth' for access problems or 'kanbanlan init' to repair setup.")
        return 1
    success("All checks passed")
    return 0


def _cmd_ensure(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    with status("Ensuring the shared board snapshot is fresh"):
        snapshot = store.ensure(provider)
    if _emit_result(
        args,
        {"snapshot_path": str(store.snapshot_path), "generated_at": snapshot["generated_at"]},
    ):
        return 0
    print(store.snapshot_path)
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    with status("Refreshing the shared board snapshot"):
        snapshot = store.refresh(provider)
    if _emit_result(
        args,
        {"snapshot_path": str(store.snapshot_path), "generated_at": snapshot["generated_at"]},
    ):
        return 0
    print(f"refreshed {store.snapshot_path} at {snapshot['generated_at']}")
    return 0


def _project_snapshot(provider: CoordinationProvider, label: str) -> dict[str, Any]:
    """Read the whole Project live without disturbing the repository cache.

    The shared cache is repository-scoped by contract, so a project-scoped read
    stays in memory. Every project scope command is read-only.
    """

    if not provider.capabilities.project_scope:
        raise RuntimeError(
            f"canonical home {provider.provider_name!r} does not support project scope"
        )
    with status(label):
        return provider.snapshot(generated_at=utc_now(), scope=SCOPE_PROJECT)


def _project_source(snapshot: dict[str, Any]) -> dict[str, Any]:
    return snapshot.get("source", {})


def _warn_linkage_problems(snapshot: dict[str, Any]) -> None:
    """Report pull requests that named a request too ambiguously to link."""

    for problem in snapshot.get("linkage_problems", []):
        warning(
            f"{problem['pull_request']} was not linked ({problem['kind']}): {problem['detail']}"
        )


def _linkage_problems_for(snapshot: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    kanbanlan_id = item.get("kanbanlan_id")
    if not kanbanlan_id:
        return []
    return [
        problem
        for problem in snapshot.get("linkage_problems", [])
        if kanbanlan_id in problem.get("kanbanlan_ids", [])
    ]


def _warn_unavailable_repositories(snapshot: dict[str, Any]) -> None:
    for entry in _project_source(snapshot).get("unavailable_repositories", []):
        warning(f"peer repository {entry['repository']} could not be read: {entry['error']}")


def _repository_status_counts(snapshot: dict[str, Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for item in snapshot.get("items", []):
        repository = item.get("repository") or "unknown"
        name = item.get("status") or "Unspecified"
        counts.setdefault(repository, {})
        counts[repository][name] = counts[repository].get(name, 0) + 1
    return {key: dict(sorted(value.items())) for key, value in sorted(counts.items())}


def _cmd_status(args: argparse.Namespace) -> int:
    if getattr(args, "project_scope", False):
        return _cmd_status_project(args)
    _, _, _, store = _context(args)
    inspection = store.inspect()
    if _emit_result(args, inspection):
        return 0
    heading("Kanbanlan status")
    field("Snapshot", status_value(inspection["snapshot_state"]))
    if inspection["generated_at"]:
        field(
            "Generated",
            f"{inspection['generated_at']} ({inspection['age_seconds']:.0f}s ago)",
        )
    counts = inspection["status_counts"]
    board = ", ".join(f"{status_value(name)}={count}" for name, count in counts.items()) or "empty"
    field("Board", board)
    next_ready = inspection["next_ready"]
    if next_ready:
        field("Next", f"{request_label(next_ready)} {next_ready['title']}")
    if inspection["error"]:
        warning(f"Last refresh: {inspection['error']['kind']}: {inspection['error']['message']}")
    return 0


def _cmd_status_project(args: argparse.Namespace) -> int:
    _, config, provider, _ = _context(args)
    snapshot = _project_snapshot(provider, "Reading every repository on the Project")
    source = _project_source(snapshot)
    per_repository = _repository_status_counts(snapshot)
    result = {
        "scope": source.get("scope"),
        "repository": config.repository,
        "repositories": source.get("repositories", []),
        "unavailable_repositories": source.get("unavailable_repositories", []),
        "generated_at": snapshot.get("generated_at"),
        "project": snapshot.get("project"),
        "status_counts": snapshot.get("status_counts", {}),
        "repository_status_counts": per_repository,
    }
    if _emit_result(args, result):
        return 0
    heading("Kanbanlan status (project scope)")
    project = snapshot.get("project", {})
    field("Project", f"{project.get('title')} (#{project.get('number')})")
    field("Generated", snapshot.get("generated_at"))
    counts = snapshot.get("status_counts", {})
    board = ", ".join(f"{status_value(name)}={count}" for name, count in counts.items()) or "empty"
    field("Board", board)
    for repository, values in per_repository.items():
        marker = repository
        if repository == config.repository:
            marker = f"{repository} (this repository)"
        summary = ", ".join(f"{status_value(name)}={count}" for name, count in values.items())
        field(marker, summary or "empty")
    _warn_unavailable_repositories(snapshot)
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    if getattr(args, "project_scope", False):
        _, _, provider, _ = _context(args)
        snapshot = _project_snapshot(provider, "Reading every repository on the Project")
    else:
        _, _, _, store = _context(args)
        snapshot = store.snapshot()
        if snapshot is None:
            raise RuntimeError("no snapshot is available; run 'kanbanlan ensure'")
    json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_overlap(args: argparse.Namespace) -> int:
    _, config, provider, _ = _context(args)
    snapshot = _project_snapshot(provider, "Checking every open card and pull request")
    source = _project_source(snapshot)
    requests = [
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "provider_ref": item.get("provider_ref"),
            "display_id": item.get("display_id"),
            "repository": item.get("repository"),
            "title": item.get("title"),
            "status": item.get("status"),
            "priority": item.get("priority"),
            "url": item.get("canonical_url") or item.get("url"),
            "active_claim": item.get("active_claim"),
            "linked_open_pull_requests": item.get("linked_open_pull_requests", []),
        }
        for item in snapshot.get("items", [])
        if item.get("type") == "ISSUE" and item.get("state") == "OPEN"
    ]
    linked_urls = {
        pull_request["url"]
        for request in requests
        for pull_request in request["linked_open_pull_requests"]
    }
    unlinked = [
        pull_request
        for pull_request in snapshot.get("open_pull_requests", [])
        if pull_request["url"] not in linked_urls
    ]
    result = {
        "scope": source.get("scope"),
        "repository": config.repository,
        "repositories": source.get("repositories", []),
        "unavailable_repositories": source.get("unavailable_repositories", []),
        "generated_at": snapshot.get("generated_at"),
        "project": snapshot.get("project"),
        "open_requests": requests,
        "unlinked_open_pull_requests": unlinked,
        "linkage_problems": snapshot.get("linkage_problems", []),
    }
    if _emit_result(args, result):
        return 0
    heading("Project overlap check")
    project = snapshot.get("project", {})
    field("Project", f"{project.get('title')} (#{project.get('number')})")
    field("Repositories", ", ".join(source.get("repositories", [])) or "none")
    _warn_unavailable_repositories(snapshot)
    _warn_linkage_problems(snapshot)
    if not requests:
        print("No open requests are on the Project.")
    for request in requests:
        section(
            f"{request['provider_ref']} [{status_value(request.get('status') or 'Unspecified')}] "
            f"{request['title']}"
        )
        if request["kanbanlan_id"]:
            field("Kanbanlan", request["kanbanlan_id"])
        claim = request.get("active_claim")
        if claim:
            owner = claim.get("session") or claim.get("author") or "unknown session"
            field("Claim", f"{owner} at {claim['claimed_at']}")
            if claim.get("touchpoints"):
                field("Touchpoints", claim["touchpoints"])
        for pull_request in request["linked_open_pull_requests"]:
            routes = ", ".join(pull_request.get("linked_by", [])) or "link"
            field(
                "Pull request",
                f"{pull_request['provider_ref']} {pull_request['url']} ({routes})",
            )
    if unlinked:
        section("Open pull requests with no linked request")
        for pull_request in unlinked:
            field(pull_request["provider_ref"], pull_request["url"])
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    _, _, _, store = _context(args)
    if _emit_result(args, {"snapshot_path": str(store.snapshot_path)}):
        return 0
    print(store.snapshot_path)
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    with status("Finding the next unblocked Ready card"):
        snapshot = store.ensure(provider)
    item = snapshot.get("next_ready")
    if not item:
        if _emit_result(args, {"request": None}):
            return 0
        print("No unblocked Ready card is available.")
        return 0
    if _emit_result(args, {"request": item}):
        return 0
    priority = item.get("priority") or "unprioritized"
    print(f"{request_label(item)} [{priority_value(priority)}] {item['title']}")
    print(item["url"])
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    with status("Loading current Project and issue state"):
        snapshot = store.refresh(provider)
        open_issues = provider.list_open_requests()
    drift = plan_reconciliation(snapshot, open_issues)
    if not args.json_output:
        _warn_linkage_problems(snapshot)
    if not drift:
        _activate_worker(_root(args), Config.load(_root(args)))
        if _emit_result(
            args,
            {
                "applied": False,
                "drift": [],
                "remaining": [],
                "linkage_problems": snapshot.get("linkage_problems", []),
            },
        ):
            return 0
        success("GitHub Issues and Project Status are reconciled.")
        return 0
    drift_payload = [asdict(value) for value in drift]
    if args.json_output and not args.apply:
        _emit_result(
            args,
            {
                "applied": False,
                "drift": drift_payload,
                "remaining": drift_payload,
                "linkage_problems": snapshot.get("linkage_problems", []),
            },
        )
        return 2
    for value in drift:
        print(format_drift(value))
    if not args.apply:
        warning(f"{len(drift)} difference(s); rerun with --apply to repair them.")
        return 2
    with status(f"Applying {len(drift)} reconciliation repair(s)"):
        remaining, _ = apply_reconciliation(provider, store, snapshot, open_issues)
    if args.json_output:
        _emit_result(
            args,
            {
                "applied": True,
                "drift": drift_payload,
                "remaining": [asdict(value) for value in remaining],
                "linkage_problems": snapshot.get("linkage_problems", []),
            },
        )
        return 1 if remaining else 0
    if remaining:
        for value in remaining:
            print(f"remaining: {format_drift(value)}")
        return 1
    _activate_worker(_root(args), Config.load(_root(args)))
    success("Reconciliation applied and verified.")
    return 0


def _discover_github_login(root: Path, hostname: str) -> str:
    result = Runner(root, env={"GH_HOST": hostname}).run(["gh", "api", "user", "--jq", ".login"])
    login = result.stdout.strip()
    if not login:
        raise RuntimeError("GitHub did not return an account login; pass --github-login explicitly")
    return login


def _activate_worker(root: Path, config: Config, *, github_login: str | None = None) -> None:
    registry = RegistryStore()
    key = common_dir(root)
    existing = registry.get(str(key))
    if existing and existing.disabled:
        return
    stable_root = primary_worktree(root)
    login = github_login or (existing.github_login if existing else None)
    if login is None:
        login = _discover_github_login(root, config.hostname)
    registration = registry.register(
        common_dir=key,
        root=stable_root,
        repository=config.repository,
        hostname=config.hostname,
        github_login=login,
    )
    if registration.enabled and not registration.disabled:
        start_worker(registry, interval_seconds=registration.interval_seconds)


def _cmd_worker(args: argparse.Namespace) -> int:
    registry = RegistryStore()
    if args.action == "status":
        payload = worker_status(registry)
        if _emit_result(args, payload):
            return 0
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.action in {"enable", "disable"}:
        root, config, _, _ = _context(args)
        key = common_dir(root)
        stable_root = primary_worktree(root)
        if args.action == "enable":
            login = args.github_login or _discover_github_login(root, config.hostname)
            registry.register(
                common_dir=key,
                root=stable_root,
                repository=config.repository,
                hostname=config.hostname,
                github_login=login,
                interval_seconds=args.interval,
            )
            registry.enable(key)
            payload = start_worker(registry, interval_seconds=args.interval)
        else:
            if registry.get(str(key)) is None:
                registry.register(
                    common_dir=key,
                    root=stable_root,
                    repository=config.repository,
                    hostname=config.hostname,
                    github_login=None,
                    interval_seconds=args.interval,
                )
            registry.disable(key)
            payload = worker_status(registry)
        if _emit_result(args, payload):
            return 0
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.action == "start":
        payload = start_worker(registry, interval_seconds=args.interval)
    elif args.action == "stop":
        payload = stop_worker(registry)
    else:
        payload = Worker(registry, interval_seconds=args.interval).run_forever(once=args.once)
    if _emit_result(args, payload or {}):
        return 0
    print(json.dumps(payload or {}, indent=2, sort_keys=True))
    return 0


def _cmd_capture(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    kanbanlan_id = new_kanbanlan_id()
    body = args.body or (
        "## Outcome\n\n"
        "<!-- Describe the independently reviewable result. -->\n\n"
        "## Acceptance criteria\n\n- [ ] "
    )
    body = attach_kanbanlan_id(body, kanbanlan_id)
    with status("Creating Inbox issue"):
        url = provider.create_request(args.title, body, args.priority)
    try:
        with status("Adding issue to the configured Project"):
            provider.add_to_projection(url)
        with status("Reconciling initial issue state"):
            snapshot = store.refresh(provider)
            remaining, refreshed = apply_reconciliation(
                provider,
                store,
                snapshot,
                provider.list_open_requests(),
            )
    except (CommandError, RuntimeError) as exc:
        raise RuntimeError(
            f"the issue was created at {url}, but Project setup failed: {exc}"
        ) from exc
    if remaining:
        raise RuntimeError("the issue was created but its Project state did not reconcile")
    item = _issue(refreshed, kanbanlan_id)
    _record_session_activity(
        config=config,
        provider=provider,
        reference=item["number"],
        action="capture",
        from_status=None,
        to_status="Inbox",
        actor=actor,
    )
    if config.session_tracking_enabled():
        store.refresh(provider)
    result = {
        "kanbanlan_id": kanbanlan_id,
        "url": url,
        "actor_session": actor.to_dict() if actor else None,
    }
    if _emit_result(args, result):
        return 0
    print(f"Kanbanlan: {kanbanlan_id}")
    print(url)
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking request {args.issue}"):
        snapshot = store.refresh(provider)
    item = _issue(snapshot, args.issue)
    number = item["number"]
    label = request_label(item)
    if item.get("state") != "OPEN":
        raise RuntimeError(f"request {label} is not open")
    if item.get("status") != "Inbox":
        raise RuntimeError(f"request {label} is {item.get('status')!r}, not Inbox")
    with status(f"Moving {label} to Ready"):
        _set_state(provider, snapshot, number, "status:ready", "Ready")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="triage",
            from_status="Inbox",
            to_status="Ready",
            actor=actor,
        )
        store.refresh(provider)
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "status": "Ready",
        "actor_session": actor.to_dict() if actor else None,
    }
    if _emit_result(args, result):
        return 0
    success(f"Moved {label} to Ready")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking request {args.issue}"):
        snapshot = store.refresh(provider)
    item = _issue(snapshot, args.issue)
    number = item["number"]
    label = request_label(item)
    if item.get("state") != "OPEN":
        raise RuntimeError(f"request {label} is not open")
    if item.get("status") != "Ready":
        raise RuntimeError(f"request {label} is {item.get('status')!r}, not Ready")
    if item.get("active_claim"):
        raise RuntimeError(f"request {label} already has an active claim")

    session = args.session or (actor.reference if actor else None)
    session = session or f"kanbanlan-{uuid.uuid4().hex[:8]}"
    branch, worktree = _claim_checkout(args, root, config, item)
    timestamp = _utc_timestamp()
    with status(f"Posting claim for {label}"):
        provider.comment_request(
            number,
            (
                f"CLAIM: {timestamp}\n"
                f"Kanbanlan: {item.get('kanbanlan_id') or 'unassigned'}\n"
                f"Session: {session}\n"
                f"Branch: {branch}\n"
                f"Worktree: {worktree}\n"
                f"Touchpoints: {args.touchpoints}"
            ),
        )
        refreshed = store.refresh(provider)
    claimed = _issue(refreshed, number).get("active_claim") or {}
    if claimed.get("session") != session:
        provider.comment_request(
            number,
            (f"RELEASED: {_utc_timestamp()} — concurrent claim lost\nSession: {session}"),
        )
        owner = claimed.get("session") or "another session"
        raise RuntimeError(f"request {label} was claimed first by {owner}")

    with status("Moving request to In progress"):
        _set_state(provider, refreshed, number, "status:in-progress", "In progress")
    try:
        if not args.no_worktree:
            with status(f"Creating worktree {worktree}"):
                _create_worktree(root, config, branch, Path(worktree))
    except Exception:
        provider.comment_request(
            number,
            (f"RELEASED: {_utc_timestamp()} — worktree creation failed\nSession: {session}"),
        )
        latest = store.refresh(provider)
        _set_state(provider, latest, number, "status:ready", "Ready")
        raise
    _record_session_activity(
        config=config,
        provider=provider,
        reference=number,
        action="claim",
        from_status="Ready",
        to_status="In progress",
        actor=actor,
        owner_session=session,
    )
    store.refresh(provider)
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "provider_ref": item.get("provider_ref"),
        "session": session,
        "actor_session": actor.to_dict() if actor else None,
        "branch": branch,
        "worktree": worktree,
    }
    if _emit_result(args, result):
        return 0
    success(f"Claimed {label} as {session}")
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
    identity = (item.get("kanbanlan_id") or str(item["number"])).lower()
    branch = args.branch or f"work/{identity}-{slug or 'request'}"
    common = Path(
        runner.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
    ).resolve()
    default_worktree = common.parent / ".worktrees" / f"{identity}-{slug or 'request'}"
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
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking active claim for request {args.issue}"):
        snapshot = store.refresh(provider)
    item = _issue(snapshot, args.issue)
    number = item["number"]
    label = request_label(item)
    claim = item.get("active_claim")
    if not claim:
        raise RuntimeError(f"request {label} has no active claim")
    session = claim.get("session") or "unknown"
    destination = "Blocked" if args.blocked else "Ready"
    with status(f"Releasing claim and moving request to {destination}"):
        provider.comment_request(
            number,
            (f"RELEASED: {_utc_timestamp()} — {args.reason}\nSession: {session}"),
        )
        latest = store.refresh(provider)
        if args.blocked:
            _set_state(provider, latest, number, "status:blocked", "Blocked")
        else:
            _set_state(provider, latest, number, "status:ready", "Ready")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="release",
            from_status="In progress",
            to_status=destination,
            actor=actor,
            owner_session=session,
        )
        store.refresh(provider)
    if _emit_result(
        args,
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "session": session,
            "status": destination,
            "actor_session": actor.to_dict() if actor else None,
        },
    ):
        return 0
    success(f"Released {label} from {session}")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking pull requests for request {args.issue}"):
        snapshot = store.refresh(provider)
    item = _issue(snapshot, args.issue)
    number = item["number"]
    label = request_label(item)
    linked = item.get("linked_open_pull_requests") or []
    if not linked:
        blocked = _linkage_problems_for(snapshot, item)
        if blocked:
            detail = "; ".join(
                f"{problem['pull_request']} ({problem['kind']}): {problem['detail']}"
                for problem in blocked
            )
            raise RuntimeError(
                f"request {label} has no linked open pull request; "
                f"an ambiguous reference was not associated: {detail}"
            )
        raise RuntimeError(f"request {label} has no linked open pull request")
    with status("Moving request to In review"):
        _set_state(provider, snapshot, number, "status:review", "In review")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="review",
            from_status=item.get("status"),
            to_status="In review",
            actor=actor,
            owner_session=(item.get("active_claim") or {}).get("session"),
        )
        store.refresh(provider)
    if _emit_result(
        args,
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "status": "In review",
            "actor_session": actor.to_dict() if actor else None,
            "linked_open_pull_requests": [
                {
                    "provider_ref": pull_request["provider_ref"],
                    "repository": pull_request["repository"],
                    "url": pull_request["url"],
                    "is_draft": pull_request.get("is_draft", False),
                    "linked_by": pull_request.get("linked_by", []),
                }
                for pull_request in linked
            ],
        },
    ):
        return 0
    success(f"Moved {label} to In review")
    for pull_request in linked:
        routes = ", ".join(pull_request.get("linked_by", [])) or "link"
        field(pull_request["provider_ref"], f"{pull_request['url']} ({routes})")
    return 0


def _cmd_handoff(args: argparse.Namespace) -> int:
    root, config, provider, store = _context(args)
    actor = _actor_session(args, root, config)
    with status(f"Checking active claim for request {args.issue}"):
        snapshot = store.refresh(provider)
    item = _issue(snapshot, args.issue)
    number = item["number"]
    label = request_label(item)
    if not item.get("active_claim"):
        raise RuntimeError(f"request {label} has no active claim")
    with status(f"Handing off {label} to {args.session}"):
        provider.comment_request(
            number,
            (
                f"HANDOFF: {_utc_timestamp()} — {args.reason}\n"
                f"Kanbanlan: {item.get('kanbanlan_id') or 'unassigned'}\n"
                f"Session: {args.session}\n"
                f"Branch: {args.branch}\n"
                f"Worktree: {Path(args.worktree).resolve()}"
            ),
        )
        refreshed = store.refresh(provider)
        _set_state(provider, refreshed, number, "status:in-progress", "In progress")
        _record_session_activity(
            config=config,
            provider=provider,
            reference=number,
            action="handoff",
            from_status="In progress",
            to_status="In progress",
            actor=actor,
            owner_session=args.session,
        )
        store.refresh(provider)
    if _emit_result(
        args,
        {
            "kanbanlan_id": item.get("kanbanlan_id"),
            "session": args.session,
            "actor_session": actor.to_dict() if actor else None,
        },
    ):
        return 0
    success(f"Handed off {label} to {args.session}")
    return 0


def _cmd_sessions(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    with status(f"Loading session activity for request {args.request}"):
        snapshot = store.ensure(provider)
    item = _issue(snapshot, args.request)
    history = _session_activity(item, action=args.action)
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "provider_ref": item.get("provider_ref"),
        "history_truncated": item.get("session_history_truncated", False),
        "events": history,
    }
    if _emit_result(args, result):
        return 0
    if not history:
        print("No matching agent session activity is recorded.")
        return 0
    for event in history:
        actor = event.get("actor")
        responsible = event.get("responsible") or actor
        display = responsible.get("display") if responsible else "unavailable"
        destination = event.get("to_status") or "no status change"
        print(f"{event['action']:<10} {destination:<12} {display}  {event.get('at') or ''}")
        command = responsible.get("resume_command") if responsible else None
        if command:
            print(f"  resume: {shlex.join(command)}")
        if actor and responsible and actor.get("reference") != responsible.get("reference"):
            print(f"  actor:  {actor.get('display')}")
    if item.get("session_history_truncated"):
        warning("Only the most recent 100 request comments were available; history may be partial.")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    _, _, provider, store = _context(args)
    with status(f"Loading resumable sessions for request {args.request}"):
        snapshot = store.ensure(provider)
    item = _issue(snapshot, args.request)
    history = _session_activity(item, action=args.action)
    resumable: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for event in history:
        responsible = event.get("responsible") or event.get("actor")
        if responsible and responsible.get("resume_command"):
            resumable.append((event, responsible))
    if not resumable:
        scope = f" for action {args.action!r}" if args.action else ""
        raise RuntimeError(f"request {request_label(item)} has no resumable agent session{scope}")
    selected, selected_session = resumable[-1]
    command = [str(value) for value in selected_session["resume_command"]]
    result = {
        "kanbanlan_id": item.get("kanbanlan_id"),
        "action": selected["action"],
        "at": selected.get("at"),
        "session": selected_session,
        "actor_session": selected.get("actor"),
        "command": command,
    }
    if args.json_output:
        _emit_result(args, result)
        return 0
    if args.run:
        os.execvp(command[0], command)
        raise AssertionError("os.execvp returned unexpectedly")
    print(shlex.join(command))
    return 0


def _cmd_session_hook(args: argparse.Namespace) -> int:
    root = _root(args)
    config = Config.load(root)
    try:
        payload = json.load(sys.stdin)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not parse agent hook JSON from stdin: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("agent hook input must be a JSON object")
    if config.session_tracking_enabled():
        harness = "grok" if os.environ.get("GROK_SESSION_ID") else args.agent
        session = session_from_hook_payload(payload, harness)
        workspaces = hook_workspaces(payload)
        cwd = payload.get("cwd")
        if not cwd and workspaces:
            cwd = workspaces[0]
        SessionContextStore(cache_dir(root)).register(
            session,
            workspaces=workspaces or [str(root)],
            cwd=str(cwd or root),
        )
    print("{}")
    return 0


def _session_activity(
    item: dict[str, Any],
    *,
    action: str | None,
) -> list[dict[str, Any]]:
    history = item.get("session_history", [])
    if action:
        return [value for value in history if value.get("action") == action]
    return list(history)


def _cmd_record(args: argparse.Namespace) -> int:
    root, _, provider, store = _context(args)
    with status(f"Loading request {args.request}"):
        snapshot = store.ensure(provider)
    item = _issue(snapshot, args.request)
    result = create_record(root, item)
    payload = {
        "action": result.action,
        "kanbanlan_id": item.get("kanbanlan_id"),
        "path": str(result.path),
    }
    if _emit_result(args, payload):
        return 0
    print(f"{result.action}: {result.path.relative_to(root)}")
    return 0


def _set_state(
    provider: CoordinationProvider,
    snapshot: dict[str, Any],
    reference: int | str,
    label: str,
    status: str,
) -> None:
    item = _issue(snapshot, reference)
    provider.set_request_status(item["number"], label)
    provider.set_projection_status(
        item["project_item_id"],
        snapshot["project"],
        status,
    )


def _issue(snapshot: dict[str, Any], reference: int | str) -> dict[str, Any]:
    return resolve_request_item(snapshot, reference)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
