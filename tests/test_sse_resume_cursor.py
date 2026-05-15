"""SSE resume cursor must advance only after the consumer accepts a frame."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from spec_cli.realtime.transport import SSEConsumer


def _minimal_turn_payload(*, event_id: int) -> str:
    return (
        '{"id":%d,"project_id":1,"session_id":"s","source":"cursor",'
        '"role":"user","branch":"main","summary":"hi","text":"hi",'
        '"author_user_id":1,"author_handle":"alice","author_name":"Alice"}'
    ) % event_id


def test_last_event_id_advances_only_after_yield_returns() -> None:
    """Parsing frame N+1 must not skip frame N if the consumer is still busy."""
    consumer = SSEConsumer(
        api_base="http://example.invalid",
        access_token="t",
        project_id=1,
    )

    lines = [
        "id: 10",
        "event: turn",
        f"data: {_minimal_turn_payload(event_id=10)}",
        "",
        "id: 11",
        "event: turn",
        f"data: {_minimal_turn_payload(event_id=11)}",
        "",
    ]

    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = iter(lines)

    with patch("spec_cli.realtime.transport.requests.get", return_value=resp):
        gen = consumer.stream()
        first = next(gen)
        assert first.id == 10
        # Suspended inside ``yield`` — must not commit id 10 yet, and
        # must not have peeked ahead to id 11.
        assert consumer._last_event_id is None  # noqa: SLF001

        second = next(gen)
        assert second.id == 11
        assert consumer._last_event_id == 10  # noqa: SLF001

        consumer.stop()
        list(gen)
        assert consumer._last_event_id == 11  # noqa: SLF001
