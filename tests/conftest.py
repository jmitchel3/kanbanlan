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
