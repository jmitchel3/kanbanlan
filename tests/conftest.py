from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Never read or write the developer's real account bindings or worker state."""

    monkeypatch.setenv("KANBANLAN_STATE_DIR", str(tmp_path_factory.mktemp("state")))
    monkeypatch.delenv("KANBANLAN_GITHUB_ACCOUNT", raising=False)
    # Never replay the developer's real legacy outbox; tests of it unset this.
    monkeypatch.setenv("KANBANLAN_SKIP_LEGACY_OUTBOX", "1")
