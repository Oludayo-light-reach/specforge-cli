"""Tail assistant stability window for ``spec watch``."""
from __future__ import annotations

import pytest

from spec_cli.realtime.watcher import (
    TAIL_ASSISTANT_STABILITY_FLOOR_SECS,
    tail_stability_quiet_secs,
)


def test_tail_stability_default_uses_floor() -> None:
    assert tail_stability_quiet_secs(2.0) == TAIL_ASSISTANT_STABILITY_FLOOR_SECS


def test_tail_stability_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEC_LIVE_TAIL_STABILITY_SECS", "300")
    assert tail_stability_quiet_secs(2.0) == 300.0
