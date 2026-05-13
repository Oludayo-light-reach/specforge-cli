"""Shared assistant snapshot merge for Spec Live Q/A and slash /turn."""

from __future__ import annotations

import json
from dataclasses import replace

from .events import IncomingEvent


def _merge_streamed_assistant_text(chunks: list[IncomingEvent]) -> str | None:
    """Join assistant ``text`` fields from multiple DB/SSE snapshots.

    Providers may emit (a) monotonically growing cumulative strings, (b) independent
    paragraphs in separate rows, or (c) a mix. Taking only the single longest row
    drops (b) and makes ``/turn`` / ``/full`` look like one-liners.

    Rules (in ``id`` order, non-empty stripped bodies only):

    - If ``nxt`` extends ``cur`` as a prefix (``nxt.startswith(cur)``), keep the
      longer cumulative snapshot.
    - If ``cur`` already contains ``nxt`` as a prefix, ignore the shorter row.
    - Otherwise flush ``cur`` and start a new segment from ``nxt``. When the next
      body is longer but not a prefix extension (revision / unrelated tail), we
      still flush the previous segment so nothing is silently discarded.
    - Same-length bodies that are not prefix-related: keep both segments (two
      equal-width paragraphs are indistinguishable from two revisions without
      extra metadata; losing text hurts ``/turn`` / ``/full`` more than an
      occasional duplicate line).
    """
    ordered = sorted(chunks, key=lambda e: e.id)
    bodies: list[str] = []
    for e in ordered:
        t = (e.text or "").strip()
        if t:
            bodies.append(t)
    if not bodies:
        return None
    if len(bodies) == 1:
        return bodies[0]

    segments: list[str] = []
    cur = bodies[0]
    for nxt in bodies[1:]:
        if nxt.startswith(cur):
            cur = nxt
        elif cur.startswith(nxt):
            pass
        elif len(nxt) > len(cur):
            segments.append(cur)
            cur = nxt
        elif len(nxt) < len(cur):
            segments.append(cur)
            cur = nxt
        else:
            # Same length, neither extends the other (e.g. two paragraphs emitted
            # as separate rows of equal width). Prefer keeping both over silently
            # dropping the earlier snapshot.
            segments.append(cur)
            cur = nxt
    segments.append(cur)
    return "\n\n".join(segments)


def merge_assistant_snapshots(chunks: list[IncomingEvent]) -> IncomingEvent:
    """Merge streaming assistant rows into one logical reply.

    Same semantics as :meth:`spec_cli.commands.team._TeamWatchQAState._merge_assistant_chunks`
    — extracted so ``/turn`` / ``/full`` can reuse without importing ``team``.
    """
    if not chunks:
        raise ValueError("merge requires non-empty chunks")
    by_id = max(chunks, key=lambda e: e.id)
    text = _merge_streamed_assistant_text(chunks)
    if text is None:
        text = (by_id.text or "").strip() or None
    summary = (by_id.summary or "").strip() or None
    if not summary:
        by_sum = max(
            chunks,
            key=lambda e: len((e.summary or "").strip()),
        )
        summary = (by_sum.summary or "").strip() or None
    merged_tools: list = []
    seen_sig: set[tuple[str, str]] = set()
    for c in sorted(chunks, key=lambda e: e.id):
        for tc in c.tool_calls or []:
            try:
                sig = (
                    tc.name,
                    json.dumps(tc.args, sort_keys=True, default=str),
                )
            except TypeError:
                sig = (tc.name, str(tc.args))
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            merged_tools.append(tc)
    return replace(
        by_id,
        text=text,
        summary=summary,
        tool_calls=merged_tools,
    )
