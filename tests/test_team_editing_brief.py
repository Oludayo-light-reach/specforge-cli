"""Tests for ``team_editing_brief`` mirror helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from spec_cli.realtime.team_editing_brief import (
    _compute_pull_alerts,
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


def test_pull_alerts_flag_same_branch_divergence():
    """The classic post-push case: @alice pushed to main, viewer is
    still at the prior commit on main. We should surface a pull
    alert with both short SHAs."""
    body = {
        "self": {
            "handle": "jon",
            "branch": "main",
            "head_commit": "aaaa111aaaa111aaaa111aaaa111aaaa111aaaa1",
        },
        "members": [
            {
                "handle": "alice",
                "branch": "main",
                "head_commit": "bbbb222bbbb222bbbb222bbbb222bbbb222bbbb2",
            }
        ],
    }
    alerts = _compute_pull_alerts(body)
    assert len(alerts) == 1
    assert alerts[0]["handle"] == "alice"
    assert alerts[0]["short_commit"] == "bbbb222"
    assert alerts[0]["self_short"] == "aaaa111"


def test_pull_alerts_ignore_different_branches():
    """Cross-branch staleness is normal: a teammate on a feature
    branch is *expected* to be at a different SHA from main. Pull
    alerts only fire for same-branch divergence."""
    body = {
        "self": {
            "handle": "jon",
            "branch": "main",
            "head_commit": "aaaa111aaaa111",
        },
        "members": [
            {
                "handle": "alice",
                "branch": "feature/auth",
                "head_commit": "bbbb222bbbb222",
            }
        ],
    }
    assert _compute_pull_alerts(body) == []


def test_pull_alerts_ignore_matching_commits():
    """Same branch, same SHA — peer is in sync, no alert."""
    body = {
        "self": {
            "handle": "jon",
            "branch": "main",
            "head_commit": "aaaa111aaaa111",
        },
        "members": [
            {
                "handle": "alice",
                "branch": "main",
                "head_commit": "aaaa111aaaa111",
            }
        ],
    }
    assert _compute_pull_alerts(body) == []


def test_pull_alerts_skip_detached_head():
    """No usable ``self.branch`` (detached HEAD, fresh clone) means
    we can't tell whether peer divergence is "pull needed" or just
    "different branch" — stay quiet rather than cry false alarms."""
    body = {
        "self": {
            "handle": "jon",
            "branch": None,
            "head_commit": "aaaa111aaaa111",
        },
        "members": [
            {
                "handle": "alice",
                "branch": "main",
                "head_commit": "bbbb222bbbb222",
            }
        ],
    }
    assert _compute_pull_alerts(body) == []


def test_render_team_editing_brief_includes_push_handoff():
    body = {
        "schema": 1,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "self": None,
        "members": [],
        "files_index": {},
        "push_requests": [
            {
                "to_handle": "jc",
                "from_handle": "alice",
                "from_display": "@alice",
                "branch": "main",
                "message": "need your WIP",
                "requested_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2026-01-01T01:00:00+00:00",
            }
        ],
    }
    md = render_team_editing_brief(body)
    assert "team-push-requests.yaml" in md
    assert "@jc" in md or "jc" in md
    assert "need your WIP" in md


def test_render_team_editing_brief_renders_pull_alerts():
    """The rendered markdown should surface a "Pull needed" section
    above the dirty-files list when a same-branch peer is ahead."""
    body = {
        "schema": 1,
        "updated_at": "2026-05-11T12:00:00+00:00",
        "self": {
            "handle": "jon",
            "branch": "main",
            "head_commit": "aaaa111aaaa111",
            "files": [],
        },
        "members": [
            {
                "handle": "alice",
                "name": "Alice",
                "branch": "main",
                "head_commit": "bbbb222bbbb222",
                "last_seen": "2026-05-11T12:00:00+00:00",
                "files": [],
            }
        ],
        "files_index": {},
    }
    md = render_team_editing_brief(body)
    assert "Pull needed" in md
    assert "@alice" in md
    assert "aaaa111" in md
    assert "bbbb222" in md
    assert "git pull" in md
