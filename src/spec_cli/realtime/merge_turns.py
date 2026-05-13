"""Shared assistant snapshot merge for Spec Live Q/A and slash /turn."""

from __future__ import annotations

import json
from dataclasses import replace

from .events import IncomingEvent


def merge_assistant_snapshots(chunks: list[IncomingEvent]) -> IncomingEvent:
    """Merge streaming assistant rows into one logical reply.

    Same semantics as :meth:`spec_cli.commands.team._TeamWatchQAState._merge_assistant_chunks`
    — extracted so ``/turn`` / ``/full`` can reuse without importing ``team``.
    """
    if not chunks:
        raise ValueError("merge requires non-empty chunks")
    by_id = max(chunks, key=lambda e: e.id)
    text_candidates = [c for c in chunks if (c.text or "").strip()]
    if text_candidates:
        text_src = max(
            text_candidates,
            key=lambda e: (len((e.text or "").strip()), e.id),
        )
        text = (text_src.text or "").strip() or None
    else:
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
