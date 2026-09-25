from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _foreground_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep post-mutation refreshes in-process, against each test's fake provider.

    A background refresh would start a real ``kanbanlan refresh`` process
    against the test's temporary repository. Tests of the background path
    remove this variable themselves.
    """

    monkeypatch.setenv("KANBANLAN_BACKGROUND_REFRESH", "0")
    # Likewise, lifecycle commands run live unless a test opts into the
    # local-first path.
    monkeypatch.setenv("KANBANLAN_WRITE_BEHIND", "0")
    monkeypatch.delenv("KANBANLAN_SYNC_EXECUTOR", raising=False)


@pytest.fixture(autouse=True)
def _isolated_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Never read or write the developer's real account bindings or registry."""

    monkeypatch.setenv("KANBANLAN_STATE_DIR", str(tmp_path_factory.mktemp("state")))
    monkeypatch.delenv("KANBANLAN_GITHUB_ACCOUNT", raising=False)
