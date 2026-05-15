"""Session ordering for ``spec watch`` producer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from spec_cli.prompts.schema import Session, Turn
from spec_cli.realtime import watcher as watcher_mod


def test_iter_local_sessions_newest_first(monkeypatch, tmp_path: Path) -> None:
    t_old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    t_new = datetime(2025, 6, 1, tzinfo=timezone.utc)

    def _fake_cursor(_paths, **kwargs):  # type: ignore[no-untyped-def]
        yield Session(
            id="cursor-old",
            source="cursor",
            turns=[Turn(role="user", text="a", at=t_old)],
            started_at=t_old,
            ended_at=t_old,
        )
        yield Session(
            id="cursor-new",
            source="cursor",
            turns=[Turn(role="user", text="b", at=t_new)],
            started_at=t_new,
            ended_at=t_new,
        )

    monkeypatch.setattr(
        watcher_mod,
        "claude_code_store_root",
        lambda: Path("/__no_such_claude_store__"),
    )
    monkeypatch.setattr(
        watcher_mod,
        "cursor_workspace_storage_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(watcher_mod, "read_cursor_sessions", _fake_cursor)
    monkeypatch.setattr(watcher_mod, "codex_transcript_store_available", lambda: False)

    ids = [s.id for s in watcher_mod._iter_local_sessions([tmp_path])]
    assert ids == ["cursor-new", "cursor-old"]
