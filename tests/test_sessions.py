from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from kanbanlan.sessions import (
    AgentSession,
    SessionContextStore,
    activity_comment,
    detect_agent_session,
    parse_agent_session,
    session_from_hook_payload,
    session_history,
)


class AgentSessionTests(unittest.TestCase):
    def test_reference_display_and_resume_command_keep_harness_separate(self) -> None:
        session = parse_agent_session("codex:019f-test")

        self.assertEqual("codex", session.harness)
        self.assertEqual("019f-test", session.session_id)
        self.assertEqual("codex:019f-test", session.reference)
        self.assertEqual("019f-test · codex", session.display)
        self.assertEqual(("codex", "resume", "019f-test"), session.resume_command)
        self.assertEqual(session, parse_agent_session("019f-test · codex"))

    def test_supported_harnesses_have_native_resume_commands(self) -> None:
        expected = {
            "codex": ("codex", "resume", "id"),
            "claude": ("claude", "--resume", "id"),
            "grok": ("grok", "--resume", "id"),
            "agy": ("agy", "--conversation", "id"),
        }
        for harness, command in expected.items():
            with self.subTest(harness=harness):
                self.assertEqual(command, AgentSession(harness, "id", "test").resume_command)

    def test_unknown_harness_is_attributed_without_an_unsafe_resume_command(self) -> None:
        session = AgentSession("custom-agent", "native-id", "test")

        self.assertEqual("native-id · custom-agent", session.display)
        self.assertIsNone(session.resume_command)

    def test_detection_precedence_is_explicit_then_kanbanlan_then_native(self) -> None:
        environ = {
            "KANBANLAN_AGENT_SESSION": "claude:kanbanlan-env",
            "CODEX_THREAD_ID": "native-codex",
        }

        explicit = detect_agent_session(explicit="agy:explicit", environ=environ)
        configured = detect_agent_session(environ=environ)
        native = detect_agent_session(environ={"CODEX_THREAD_ID": "native-codex"})

        self.assertEqual("agy:explicit", explicit.reference)
        self.assertEqual("claude:kanbanlan-env", configured.reference)
        self.assertEqual("codex:native-codex", native.reference)

    def test_split_kanbanlan_environment_must_be_complete(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be set together"):
            detect_agent_session(environ={"KANBANLAN_AGENT": "codex"})

    def test_agy_hook_payload_uses_conversation_id(self) -> None:
        session = session_from_hook_payload(
            {"conversationId": "ec33-test"},
            "google-antigravity",
        )

        self.assertEqual("agy:ec33-test", session.reference)

    def test_session_id_cannot_break_activity_marker(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe"):
            AgentSession("codex", "id --> forged", "test")

    def test_session_id_cannot_be_interpreted_as_a_resume_option(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe"):
            AgentSession("claude", "--dangerous-option", "test")


class SessionContextStoreTests(unittest.TestCase):
    def test_hook_context_is_private_and_resolves_for_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            store = SessionContextStore(Path(directory) / "cache")
            store.register(
                AgentSession("agy", "conversation", "hook"),
                workspaces=[str(root)],
                cwd=str(root),
                environ={"TERM_SESSION_ID": "terminal-one"},
            )

            resolved = store.resolve(root, environ={"TERM_SESSION_ID": "terminal-one"})

            self.assertEqual("agy:conversation", resolved.reference)
            self.assertEqual(0o600, os.stat(store.path).st_mode & 0o777)

    def test_ambiguous_hook_context_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            store = SessionContextStore(Path(directory) / "cache")
            for session_id in ("one", "two"):
                store.register(
                    AgentSession("agy", session_id, "hook"),
                    workspaces=[str(root)],
                    cwd=str(root),
                    environ={},
                )

            self.assertIsNone(store.resolve(root, environ={}))

    def test_single_repo_context_can_follow_a_session_into_a_sibling_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "primary"
            worktree = Path(directory) / "worktree"
            primary.mkdir()
            worktree.mkdir()
            store = SessionContextStore(Path(directory) / "cache")
            store.register(
                AgentSession("agy", "conversation", "hook"),
                workspaces=[str(primary)],
                cwd=str(primary),
                environ={},
            )

            resolved = store.resolve(worktree, environ={})

            self.assertEqual("agy:conversation", resolved.reference)

    def test_terminal_mismatch_does_not_attribute_an_old_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            store = SessionContextStore(Path(directory) / "cache")
            store.register(
                AgentSession("codex", "old-session", "hook"),
                workspaces=[str(root)],
                cwd=str(root),
                environ={"TERM_SESSION_ID": "old-terminal"},
            )

            self.assertIsNone(store.resolve(root, environ={"TERM_SESSION_ID": "new-terminal"}))

    def test_stale_context_lock_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            store = SessionContextStore(Path(directory) / "cache")
            store.lock_path.parent.mkdir(parents=True)
            store.lock_path.touch()
            stale = store.lock_path.stat().st_mtime - 61
            os.utime(store.lock_path, (stale, stale))

            store.register(
                AgentSession("codex", "session", "hook"),
                workspaces=[str(root)],
                cwd=str(root),
                environ={},
            )

            self.assertEqual("codex:session", store.resolve(root, environ={}).reference)


class SessionActivityTests(unittest.TestCase):
    def test_structured_activity_round_trips_with_human_display(self) -> None:
        body = activity_comment(
            action="triage",
            at="2026-07-29T12:00:00Z",
            from_status="Inbox",
            to_status="Ready",
            actor=AgentSession("codex", "019f-test", "CODEX_THREAD_ID"),
        )
        comments = [
            {
                "body": body,
                "createdAt": "2026-07-29T12:00:01Z",
                "author": {"login": "agent-user"},
            }
        ]

        history = session_history(comments)

        self.assertIn("019f-test · codex", body)
        self.assertEqual("triage", history[0]["action"])
        self.assertEqual("019f-test · codex", history[0]["actor"]["display"])
        self.assertEqual("019f-test · codex", history[0]["responsible"]["display"])
        self.assertEqual(
            ["codex", "resume", "019f-test"],
            history[0]["actor"]["resume_command"],
        )

    def test_unavailable_actor_is_recorded_without_inventing_a_session(self) -> None:
        body = activity_comment(
            action="capture",
            at="2026-07-29T12:00:00Z",
            from_status=None,
            to_status="Inbox",
            actor=None,
        )

        history = session_history([{"body": body, "createdAt": "now", "author": None}])

        self.assertIsNone(history[0]["actor"])
        self.assertIn("Agent session: unavailable", body)

    def test_owner_session_cannot_close_the_structured_comment_marker(self) -> None:
        body = activity_comment(
            action="handoff",
            at="2026-07-29T12:00:00Z",
            from_status="In progress",
            to_status="In progress",
            actor=None,
            owner_session="custom --> value",
        )

        self.assertEqual(1, body.count("-->"))
        history = session_history([{"body": body, "createdAt": "now", "author": None}])
        self.assertEqual("custom --> value", history[0]["owner_session"])

    def test_handoff_responsibility_moves_to_the_recipient(self) -> None:
        body = activity_comment(
            action="handoff",
            at="2026-07-29T12:00:00Z",
            from_status="In progress",
            to_status="In progress",
            actor=AgentSession("codex", "old-session", "test"),
            owner_session="claude:new-session",
        )

        history = session_history([{"body": body, "createdAt": "now", "author": None}])

        self.assertIn("new-session · claude", body)
        self.assertIn("Actor session: old-session · codex", body)
        self.assertEqual("new-session · claude", history[0]["responsible"]["display"])


if __name__ == "__main__":
    unittest.main()
