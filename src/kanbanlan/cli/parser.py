"""The argument parser for every Kanbanlan command."""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from typing import Any

from kanbanlan import __version__
from kanbanlan.scaffold import PRIORITY_LABELS
from kanbanlan.ui import error

# The commands top-level help lists; everything else lives under 'admin'.
VISIBLE_COMMANDS = ("init", "start", "done", "drop", "status", "next", "doctor", "admin")


ADMIN_COMMANDS = (
    "auth",
    "upgrade",
    "ensure",
    "refresh",
    "snapshot",
    "path",
    "overlap",
    "reconcile",
    "cleanup",
    "capture",
    "triage",
    "claim",
    "release",
    "rehome",
    "review",
    "close",
    "handoff",
    "sessions",
    "resume",
    "session-hook",
    "record",
    "worker",
    "account",
)


COMMAND_NAMES = (*VISIBLE_COMMANDS, *ADMIN_COMMANDS)


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
            "  kanbanlan init                          Configure this repository\n"
            "  kanbanlan start 'Fix login' --touchpoints auth.py\n"
            "                                          Capture or pick up a request and claim it\n"
            "  kanbanlan done                          Open the pull request and move to review\n"
            "  kanbanlan drop                          Release this worktree's claim\n"
            "  kanbanlan status                        Show the board summary\n"
            "  kanbanlan admin --help                  Lower-level commands"
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
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{" + ",".join(VISIBLE_COMMANDS) + "}",
    )

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

    start = commands.add_parser(
        "start",
        help="capture or pick up a request, then claim it in a new worktree",
        description=(
            "Resolve a Kanbanlan ID or #number to its request, or capture a new request "
            "with the given title; triage it to Ready, check the Project for other active "
            "claims, prune orphaned request worktrees, and claim it. The worktree path is "
            "printed last."
        ),
    )
    start.add_argument("request", help="Kanbanlan ID, #number, or the title of a new request")
    start.add_argument(
        "--touchpoints",
        default="unspecified",
        help="expected files or systems to change",
    )
    start.add_argument("--session", help="claim owner; defaults to the detected agent session")
    start.add_argument("--body", default="", help="body for a newly captured request")
    start.add_argument(
        "--priority",
        choices=tuple(PRIORITY_LABELS),
        default="priority:p2",
        help="priority for a newly captured request (default: priority:p2)",
    )
    _add_actor_session_argument(start)

    done = commands.add_parser(
        "done",
        help="open the pull request for this worktree's request and move it to review",
        description=(
            "Find the open pull request for the request's branch, creating one from the "
            "repository's pull request template when none exists, then move the request "
            "to In review."
        ),
    )
    done.add_argument(
        "request",
        nargs="?",
        help="Kanbanlan ID or #number; defaults to the request this worktree claims",
    )
    done.add_argument("--title", help="title for a new pull request; defaults to the request's")
    done.add_argument("--draft", action="store_true", help="open a new pull request as a draft")
    _add_actor_session_argument(done)

    drop = commands.add_parser(
        "drop",
        help="release this worktree's claim, or close the request with --close",
    )
    drop.add_argument(
        "request",
        nargs="?",
        help="Kanbanlan ID or #number; defaults to the request this worktree claims",
    )
    drop.add_argument("--reason", help="why the claim ends; required with --close")
    drop.add_argument(
        "--close", action="store_true", help="close the request instead of releasing it"
    )
    drop.add_argument(
        "--not-planned",
        action="store_true",
        help="with --close, record the request as dropped rather than delivered",
    )
    drop.add_argument(
        "--force",
        action="store_true",
        help="with --close, close even while a linked pull request is still open",
    )
    _add_actor_session_argument(drop)

    status_command = commands.add_parser("status", help="show local cache and board summary")
    _add_project_scope_argument(status_command)
    commands.add_parser("next", help="show the first unblocked Ready card")
    commands.add_parser("doctor", help="check local config, auth, fields, and labels")

    admin = commands.add_parser(
        "admin",
        help="lower-level commands for snapshots, lifecycle steps, and maintenance",
        description="Lower-level Kanbanlan commands. Each also runs without the 'admin' prefix.",
    )
    _add_admin_commands(
        admin.add_subparsers(dest="admin_command", required=True, metavar="COMMAND"),
        listed=True,
    )
    # The same commands stay reachable at the top level, unlisted, so existing
    # scripts, hooks, and generated instructions keep working.
    _add_admin_commands(commands, listed=False)
    return parser


def _add_admin_commands(commands: Any, *, listed: bool) -> None:
    """Register every admin command, listed in help only under 'admin'."""

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        if listed:
            return commands.add_parser(name, help=help_text, description=help_text)
        # A subparser added without help is left out of the command list.
        return commands.add_parser(name, description=help_text)

    add("auth", "repair GitHub login and Project scope")
    add("upgrade", "upgrade Kanbanlan to the latest release")
    add("ensure", "ensure the shared snapshot is fresh")
    refresh_command = add("refresh", "refresh the shared snapshot now")
    _add_project_scope_argument(refresh_command)
    snapshot_command = add("snapshot", "print the current snapshot JSON")
    _add_project_scope_argument(snapshot_command)
    add("path", "print the shared snapshot path")
    add("overlap", "list open requests and pull requests across the whole Project")

    reconcile = add("reconcile", "report label/claim/PR/Project drift")
    reconcile.add_argument("--apply", action="store_true", help="apply the displayed repairs")

    cleanup = add("cleanup", "remove worktrees left behind by requests that are no longer claimed")
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="perform the displayed removals instead of only planning them",
    )
    cleanup.add_argument(
        "--force",
        action="store_true",
        help="also remove a worktree with uncommitted changes or unmerged commits",
    )

    capture = add("capture", "create an Inbox request card")
    capture.add_argument("title", help="issue title")
    capture.add_argument("--body", default="", help="issue body; defaults to an outcome template")
    # Set when a legacy queued capture is replayed, so it keeps the ID it was given.
    capture.add_argument("--kanbanlan-id", help=argparse.SUPPRESS)
    capture.add_argument(
        "--priority",
        choices=tuple(PRIORITY_LABELS),
        default="priority:p2",
        help="request priority (default: priority:p2)",
    )
    capture.add_argument(
        "--repository",
        help=(
            "create the request in OWNER/REPO instead of this repository, "
            "keeping the configured Project"
        ),
    )
    _add_actor_session_argument(capture)

    triage = add("triage", "move one Inbox request to Ready")
    triage.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    _add_actor_session_argument(triage)

    claim = add("claim", "claim one Ready issue")
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

    release = add("release", "release an active claim")
    release.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    release.add_argument("--reason", required=True, help="why the claim is being released")
    release.add_argument("--blocked", action="store_true", help="move to Blocked instead of Ready")
    _add_actor_session_argument(release)

    rehome = add("rehome", "move a request to another repository without changing its identity")
    rehome.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    rehome.add_argument(
        "--repository",
        required=True,
        help="destination repository as OWNER/REPO",
    )
    rehome.add_argument(
        "--apply",
        action="store_true",
        help="perform the displayed move instead of only planning it",
    )
    _add_actor_session_argument(rehome)

    review = add("review", "move an issue with an open PR to review")
    review.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    _add_actor_session_argument(review)

    close = add("close", "close a request and move it to Done")
    close.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    close.add_argument("--reason", required=True, help="why the request is closing")
    close.add_argument(
        "--not-planned",
        action="store_true",
        help="record the request as dropped rather than delivered",
    )
    close.add_argument(
        "--force",
        action="store_true",
        help="close the request even while a linked pull request is still open",
    )
    _add_actor_session_argument(close)

    handoff = add("handoff", "transfer an active claim")
    handoff.add_argument("issue", help="Kanbanlan ID or canonical provider reference")
    handoff.add_argument("--session", required=True, help="new owner/session identifier")
    handoff.add_argument("--branch", required=True, help="branch the new owner should continue")
    handoff.add_argument("--worktree", required=True, help="worktree the new owner should continue")
    handoff.add_argument("--reason", required=True, help="handoff context")
    _add_actor_session_argument(handoff)

    sessions = add(
        "sessions", "show agent sessions that performed lifecycle activity on one request"
    )
    sessions.add_argument("request", help="Kanbanlan ID or canonical provider reference")
    sessions.add_argument("--action", help="only show one lifecycle action")

    resume = add("resume", "print or run the latest recorded agent resume command for one request")
    resume.add_argument("request", help="Kanbanlan ID or canonical provider reference")
    resume.add_argument("--action", help="select the latest session for one lifecycle action")
    resume.add_argument(
        "--run",
        action="store_true",
        help="replace Kanbanlan with the native agent resume command",
    )

    session_hook = add(
        "session-hook", "register provider-native session context from an agent lifecycle hook"
    )
    session_hook.add_argument(
        "--agent",
        required=True,
        help="agent harness supplying the hook payload (codex, claude, grok, or agy)",
    )

    record = add("record", "create a durable repository record for one request")
    record.add_argument("request", help="Kanbanlan ID or canonical provider reference")
    record.add_argument(
        "--from-pr",
        action="store_true",
        help=(
            "write or update the record from the linked pull request's Decisions, "
            "Verification, and Result sections"
        ),
    )

    worker = add("worker", "remove the background worker an older Kanbanlan installed")
    worker.add_argument(
        "action",
        choices=("uninstall",),
        help="unload and delete the worker's launchd or systemd unit and state",
    )

    account = add("account", "show or bind the gh account Kanbanlan acts as for this repository")
    account.add_argument(
        "action",
        nargs="?",
        choices=("show", "use", "clear"),
        default="show",
        help="show the resolved account (default), bind one, or remove a binding",
    )
    account.add_argument("login", nargs="?", help="gh account login for 'use'")
    account.add_argument(
        "--owner",
        action="store_true",
        help="bind every repository of this repository's owner, not just this one",
    )
