"""
HTTP transport for Spec Live: poster (sync POST) and SSE consumer (long-lived
streaming GET with reconnect).

Both built on top of ``requests`` — the same dep ``CloudClient`` already
uses — to avoid pulling in a second HTTP client. The SSE parser is ~50
lines of stdlib and handles the format spec faithfully:

  * lines beginning with ``:`` are comments (used for keepalives) — ignored.
  * ``event: <name>`` sets the type for the next dispatch (we use ``turn``).
  * ``data: <text>`` lines accumulate; multiple ``data:`` lines on one
    event are joined with ``\\n``.
  * ``id: <int>`` sets the event id; we track the last one for resume.
  * ``retry: <ms>`` updates the reconnect delay.
  * an empty line dispatches the buffered event.

On a network drop the consumer reconnects automatically with
``Last-Event-ID: <last_id>`` so the server can replay missed events
before resuming the live tail.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

import requests

from .events import IncomingEvent, OutgoingEvent

log = logging.getLogger(__name__)

# Networking knobs. Sized for a single-instance backend on commodity
# hardware; we err on the side of patience because any time saved on
# aggressive timeouts comes back as user-visible reconnect noise.
POST_TIMEOUT_SECS = 15.0
STREAM_CONNECT_TIMEOUT_SECS = 15.0
STREAM_READ_TIMEOUT_SECS = 60.0  # > server keepalive interval (15s)

# Reconnect backoff. Server sends ``retry: 5000`` on connect; the client
# starts there and doubles on consecutive failures up to the cap.
RECONNECT_BASE_DELAY_SECS = 5.0
RECONNECT_MAX_DELAY_SECS = 60.0


class SSEStreamError(RuntimeError):
    """Raised when the SSE consumer cannot make any progress at all
    (auth rejected, project not found, etc.). Transient network errors
    do *not* raise — they trigger a reconnect."""


@dataclass
class _ParsedEvent:
    """Internal — one parsed SSE frame."""

    id: int | None
    event: str | None
    data: str | None


class HTTPPoster:
    """POST one event at a time to ``/api/projects/{id}/prompt-events``.

    Wraps a single ``requests.Session`` so we get HTTP keep-alive and
    don't pay the TLS handshake on every turn. Errors are caught and
    logged; the producer loop continues — losing one event in transit
    is preferable to crashing the daemon over a 502.
    """

    def __init__(
        self,
        api_base: str,
        access_token: str,
        project_id: int,
        *,
        user_agent: str = "spec-cli/live",
    ) -> None:
        self._url = (
            f"{api_base.rstrip('/')}/api/projects/{project_id}/prompt-events"
        )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def send(self, event: OutgoingEvent, *, timeout: float | None = None) -> bool:
        """POST one event. Returns True on success, False on any error.

        Caller decides what to do on failure (retry next tick, advance
        the cursor anyway, etc.). For ``spec watch`` we *don't* advance
        the cursor on failure so the next poll re-attempts; eventual
        consistency is fine for telemetry.

        ``timeout`` overrides :data:`POST_TIMEOUT_SECS` for this call.
        Callers in shutdown paths use a tighter value (a few seconds)
        so a hung server can't stall the daemon's exit.
        """
        try:
            r = self._session.post(
                self._url,
                json=event.to_json(),
                timeout=timeout if timeout is not None else POST_TIMEOUT_SECS,
            )
        except requests.RequestException as e:
            log.warning("spec-live: post failed (network): %s", e)
            return False
        if r.status_code >= 400:
            body = r.text[:200]
            log.warning(
                "spec-live: post rejected (%s): %s",
                r.status_code,
                body,
            )
            return False
        return True

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass


class SSEConsumer:
    """Long-lived SSE listener with automatic reconnect.

    Run :meth:`stream` from a background thread; it yields
    :class:`IncomingEvent` instances forever (until ``stop`` is set).
    Reconnect logic is encapsulated — callers don't need to handle
    transient drops, only fatal errors raised as
    :class:`SSEStreamError`.
    """

    def __init__(
        self,
        api_base: str,
        access_token: str,
        project_id: int | None = None,
        *,
        workspace: bool = False,
        include_presence: bool = False,
        user_agent: str = "spec-cli/live",
    ) -> None:
        base = api_base.rstrip("/")
        if workspace:
            self._url = f"{base}/api/me/prompt-stream"
            if include_presence:
                self._url += "?include_presence=true"
        else:
            if project_id is None:
                raise ValueError("project_id is required unless workspace=True")
            self._url = f"{base}/api/projects/{project_id}/prompt-stream"
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": user_agent,
            "Accept": "text/event-stream",
            "Cache-Control": "no-store",
        }
        self._last_event_id: int | None = None
        self._retry_delay = RECONNECT_BASE_DELAY_SECS
        self._stop = threading.Event()
        # Reference to the live ``requests.Response`` for the current
        # connection, guarded by ``_resp_lock``. Held so :meth:`stop`
        # can force-close the underlying socket from another thread,
        # which makes a blocking ``iter_lines()`` raise immediately
        # instead of waiting up to ``STREAM_READ_TIMEOUT_SECS`` for
        # the next byte. Without this, a Ctrl+C while the stream is
        # idle would feel hung.
        self._resp_lock = threading.Lock()
        self._active_response: requests.Response | None = None

    def set_resume_cursor(self, last_event_id: int | None) -> None:
        """Set the ``Last-Event-ID`` value to send on the next connect.
        Called once at startup with the value persisted on disk so the
        consumer resumes where the previous run left off."""
        if last_event_id is not None and last_event_id >= 0:
            self._last_event_id = last_event_id

    def stop(self) -> None:
        """Tell the consumer to wind down at the next opportunity.

        Idempotent and thread-safe. We set the flag *and* eagerly close
        the active streaming response (if any) so the reader thread
        exits at the boundary of the next ``recv`` instead of waiting
        out the read timeout. ``requests.Response.close`` is documented
        as safe to call from any thread — it shuts the underlying
        socket and causes the iterator to raise, which the consumer
        then treats as a clean stream-ended signal.
        """
        self._stop.set()
        with self._resp_lock:
            resp = self._active_response
            self._active_response = None
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def stream(self) -> Iterator[IncomingEvent]:
        """Yield events forever (until :meth:`stop` is called).

        Reconnect-on-error is built in; the only way to exit is by
        calling :meth:`stop` (which drops out cleanly on the next
        iteration boundary) or by a fatal authentication-class error
        that raises :class:`SSEStreamError`.
        """
        while not self._stop.is_set():
            try:
                yield from self._connect_once()
                # Clean exit (server closed) — reset backoff before
                # reconnecting so a brief blip doesn't pile up.
                self._retry_delay = RECONNECT_BASE_DELAY_SECS
            except SSEStreamError:
                # Fatal — propagate so the caller can show a useful
                # error and exit the watcher.
                raise
            except (requests.RequestException, ConnectionError, OSError) as e:
                log.info(
                    "spec-live: stream error (%s) — reconnect in %.1fs",
                    e,
                    self._retry_delay,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "spec-live: unexpected stream error (%s) — reconnect in %.1fs",
                    e,
                    self._retry_delay,
                )
            if self._stop.is_set():
                return
            self._sleep_with_stop(self._retry_delay)
            # Exponential backoff with cap.
            self._retry_delay = min(
                RECONNECT_MAX_DELAY_SECS, max(self._retry_delay, 1.0) * 2.0
            )

    # -- internals -----------------------------------------------------

    def _connect_once(self) -> Iterator[IncomingEvent]:
        headers = dict(self._headers)
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = str(self._last_event_id)

        # Bail before opening the socket if stop() raced us — we
        # don't want to start a fresh connection only to immediately
        # tear it down.
        if self._stop.is_set():
            return

        resp = requests.get(
            self._url,
            headers=headers,
            stream=True,
            timeout=(STREAM_CONNECT_TIMEOUT_SECS, STREAM_READ_TIMEOUT_SECS),
        )
        # Publish the live response *before* parsing so a stop() on
        # another thread can close it mid-read.
        with self._resp_lock:
            self._active_response = resp
        try:
            if resp.status_code in (401, 403):
                raise SSEStreamError(
                    f"prompt-stream auth rejected ({resp.status_code}). "
                    "Run `spec login` again."
                )
            if resp.status_code == 404 or resp.status_code == 400:
                raise SSEStreamError(
                    f"prompt-stream rejected: {resp.status_code} — "
                    "is the project resolvable from this account?"
                )
            if resp.status_code >= 400:
                raise SSEStreamError(
                    f"prompt-stream returned {resp.status_code}: "
                    f"{resp.text[:200]}"
                )

            for parsed in _iter_sse_frames(resp.iter_lines(decode_unicode=True)):
                if self._stop.is_set():
                    return
                if parsed.id is not None:
                    self._last_event_id = parsed.id
                if parsed.event == "turn" and parsed.data:
                    try:
                        payload = json.loads(parsed.data)
                    except (TypeError, ValueError) as e:
                        log.debug("spec-live: dropping malformed frame: %s", e)
                        continue
                    try:
                        event = IncomingEvent.from_json(payload)
                    except (KeyError, TypeError, ValueError) as e:
                        log.debug("spec-live: dropping unparseable event: %s", e)
                        continue
                    yield event
        finally:
            with self._resp_lock:
                # Clear the published reference whether we exited
                # cleanly or via close-from-stop. ``resp.close`` is
                # idempotent so the redundant call below is safe.
                self._active_response = None
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _sleep_with_stop(self, secs: float) -> None:
        """Wait ``secs`` seconds, but bail early if :meth:`stop` is
        called. Granularity is 0.5s — fine for reconnect timers, much
        smaller than typical SSE ``retry:`` values."""
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return
            time.sleep(0.5)


def _iter_sse_frames(lines: Iterator[str]) -> Iterator[_ParsedEvent]:
    """Parse the SSE wire format one frame at a time.

    Generators on top of ``requests.Response.iter_lines``; works on
    both LF and CRLF, ignores comment frames, joins multi-line
    ``data:`` blocks with ``\\n`` per the spec.
    """
    buf_id: int | None = None
    buf_event: str | None = None
    buf_data_parts: list[str] = []
    saw_any_field = False

    def _flush() -> _ParsedEvent | None:
        nonlocal buf_id, buf_event, buf_data_parts, saw_any_field
        if not saw_any_field:
            return None
        evt = _ParsedEvent(
            id=buf_id,
            event=buf_event,
            data="\n".join(buf_data_parts) if buf_data_parts else None,
        )
        buf_id = None
        buf_event = None
        buf_data_parts = []
        saw_any_field = False
        return evt

    for raw in lines:
        if raw is None:
            continue
        # Empty line terminates an event.
        if raw == "":
            evt = _flush()
            if evt is not None:
                yield evt
            continue
        # Comment line (used for keepalives).
        if raw.startswith(":"):
            saw_any_field = True  # keep us in "we're inside a frame" so flush is a noop dispatch only when fields are set
            continue
        # ``field`` or ``field: value``. Per spec, the colon may be
        # absent (rare) — treat the whole line as a field name with
        # empty value.
        if ":" in raw:
            field, _, value = raw.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = raw, ""
        field = field.strip()
        if not field:
            continue
        saw_any_field = True
        if field == "data":
            buf_data_parts.append(value)
        elif field == "event":
            buf_event = value or None
        elif field == "id":
            try:
                buf_id = int(value)
            except (TypeError, ValueError):
                buf_id = None
        elif field == "retry":
            # We don't honor server-suggested retry directly here —
            # the consumer manages backoff itself. Logged for debugging.
            log.debug("spec-live: server retry hint = %s", value)
        # Unknown fields per spec are ignored.

    # Final flush in case the stream ends mid-event without a trailing blank.
    evt = _flush()
    if evt is not None:
        yield evt


def run_consumer_in_thread(
    consumer: SSEConsumer,
    on_event: Callable[[IncomingEvent], None],
    on_fatal: Callable[[SSEStreamError], None],
) -> threading.Thread:
    """Convenience: spin up the SSE consumer on a daemon thread.

    The thread terminates cleanly when :meth:`SSEConsumer.stop` is
    called or when ``on_fatal`` is invoked. ``on_event`` is called from
    the consumer thread — callers that update terminal output must
    serialise themselves (the ``Notifier`` does, via Rich's console
    lock).
    """

    def _run() -> None:
        try:
            for event in consumer.stream():
                try:
                    on_event(event)
                except Exception as e:  # noqa: BLE001
                    log.warning("spec-live: handler raised on event %s: %s", event.id, e)
        except SSEStreamError as e:
            on_fatal(e)

    t = threading.Thread(target=_run, name="spec-live-sse", daemon=True)
    t.start()
    return t


__all__ = [
    "HTTPPoster",
    "SSEConsumer",
    "SSEStreamError",
    "run_consumer_in_thread",
]
