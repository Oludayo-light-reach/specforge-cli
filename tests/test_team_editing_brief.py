"""Tests for ``team_editing_brief`` mirror helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from spec_cli.realtime.team_editing_brief import (
    render_team_editing_brief,
    team_presence_mirror_stale,
)


def test_team_presence_mirror_stale_missing_updated_at():
    assert team_presence_mirror_stale({}) is True
    assert team_presence_mirror_stale({"updated_at": ""}) is True


def test_team_presence_mirror_stale_fresh():
    now = datetime.now(timezone.utc).isoformat()
    assert team_presence_mirror_stale({"updated_at": now}, max_age_secs=3600) is False


def test_team_presence_mirror_stale_old():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert team_presence_mirror_stale({"updated_at": old}, max_age_secs=900) is True


def test_render_team_editing_brief_lists_files():
    body = {
        "schema": 1,
        "updated_at": "2026-05-11T12:00:00+00:00",
        "self": None,
        "members": [
            {
                "handle": "alice",
                "name": "Alice",
                "branch": "main",
                "last_seen": "2026-05-11T12:00:00+00:00",
                "files": [{"path": "a.py", "lines_added": 2, "lines_removed": 0, "untracked": False}],
            }
        ],
        "files_index": {
            "a.py": [{"handle": "alice", "self": False, "lines_added": 2, "lines_removed": 0}]
        },
    }
    md = render_team_editing_brief(body)
    assert "a.py" in md
    assert "@alice" in md
