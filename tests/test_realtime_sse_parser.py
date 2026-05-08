"""Tests for the SSE wire-format parser in ``spec_cli.realtime.transport``.

The parser is small but the SSE spec has enough corner cases that a
hand-written implementation needs explicit coverage:

* multiple ``data:`` lines on one event must be joined with ``\\n``
* ``id:`` lines must update the emitted event's id
* comment lines (``: …``) must be ignored without breaking the
  surrounding event
* missing trailing newline must still flush the last event
"""
from __future__ import annotations

from spec_cli.realtime.transport import _iter_sse_frames


def test_parses_single_event():
    lines = [
        "id: 7",
        "event: turn",
        'data: {"k":"v"}',
        "",
    ]
    frames = list(_iter_sse_frames(iter(lines)))
    assert len(frames) == 1
    f = frames[0]
    assert f.id == 7
    assert f.event == "turn"
    assert f.data == '{"k":"v"}'


def test_multiline_data_joined_with_newline():
    lines = [
        "event: turn",
        "data: line1",
        "data: line2",
        "data: line3",
        "",
    ]
    frames = list(_iter_sse_frames(iter(lines)))
    assert len(frames) == 1
    assert frames[0].data == "line1\nline2\nline3"


def test_comment_lines_are_ignored():
    lines = [
        ": this is a comment",
        ": another keepalive",
        "id: 12",
        "data: hi",
        "",
    ]
    frames = list(_iter_sse_frames(iter(lines)))
    # The comment-only frames between events are absorbed; the real
    # event still emits.
    assert any(f.id == 12 and f.data == "hi" for f in frames)


def test_keepalive_only_does_not_emit_event():
    lines = [
        ": keepalive",
        "",
        ": keepalive",
        "",
    ]
    frames = [f for f in _iter_sse_frames(iter(lines)) if f.data is not None]
    assert frames == []


def test_multiple_events_in_sequence():
    lines = [
        "id: 1",
        "event: turn",
        "data: a",
        "",
        "id: 2",
        "event: turn",
        "data: b",
        "",
    ]
    frames = list(_iter_sse_frames(iter(lines)))
    real = [f for f in frames if f.data is not None]
    assert [f.id for f in real] == [1, 2]
    assert [f.data for f in real] == ["a", "b"]


def test_field_without_colon_is_treated_as_empty_value():
    """SSE spec: a line with no colon is the field name with no value."""
    lines = [
        "data",  # no colon -- empty value
        "data: real",
        "",
    ]
    frames = [f for f in _iter_sse_frames(iter(lines)) if f.data is not None]
    assert len(frames) == 1
    # First (empty) data + second (real) data joined with \n
    assert frames[0].data == "\nreal"


def test_invalid_id_is_dropped_to_none():
    lines = [
        "id: not-a-number",
        "data: payload",
        "",
    ]
    frames = [f for f in _iter_sse_frames(iter(lines)) if f.data is not None]
    assert frames[0].id is None
    assert frames[0].data == "payload"


def test_trailing_event_without_blank_line_still_flushes():
    """A stream that ends mid-event should still surface the buffered
    frame so we don't silently drop the last bit before reconnect."""
    lines = [
        "id: 99",
        "event: turn",
        "data: tail",
        # no terminating empty line
    ]
    frames = [f for f in _iter_sse_frames(iter(lines)) if f.data is not None]
    assert len(frames) == 1
    assert frames[0].id == 99
    assert frames[0].data == "tail"
