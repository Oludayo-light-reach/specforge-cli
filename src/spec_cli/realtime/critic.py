"""
Spec Live auto-critic — pattern-matching review of incoming prompts.

This is the "senior engineer watching over your shoulder" layer. We
do **not** call an LLM here on purpose: per-event latency must be
tiny (microseconds), the rules must be debuggable line-by-line, and
the receiver must be able to run this on every workstation watching
``spec team watch`` without burning tokens.

The rules below encode the failure modes we have actually observed
when AI engineering teams pair with agents in real time:

* **Vague intent.** Prompts whose verb has no scope ("fix this",
  "improve the code", "clean up") tend to produce drive-by edits
  with no acceptance criteria — exactly the kind of work that
  silently regresses three weeks later.

* **Destructive verbs.** ``rm``, ``drop table``, ``wipe``, ``reset
  --hard``, ``DELETE FROM``, etc. These should always be a
  pause-and-confirm moment.

* **Test-bypass language.** "Disable the tests", "skip this test",
  "comment out the failing assertion". The AI will happily do this
  and it is almost always the wrong move.

* **Secrets in the prompt.** Pasted API keys, bearer tokens, private
  RSA blocks. A teammate should warn the user before the prompt
  reaches Cloud (we also redact at broadcast time, but a reviewer
  should still know).

* **Trust phrases.** "You decide", "just do whatever", "make it
  good". These leak human judgement to the model when the human
  hasn't actually decided yet.

* **Multi-task in one prompt.** "Add the API, write the tests, and
  also update the docs". Conjunction prompts make review and rollback
  hard — split them.

Each rule fires at most one critique per event so the stream doesn't
turn into a wall of yellow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .events import IncomingEvent


# Severity tiers. ``warn`` is the default ⚠ glyph in the Notifier;
# ``high`` upgrades to a louder ⛔ and a recommended ``block`` flag
# kind in the suggestion. ``info`` is a soft nudge (no glyph change).
SEV_INFO = "info"
SEV_WARN = "warn"
SEV_HIGH = "high"

# Map a critique severity to the flag kind we would recommend the
# human reviewer post. Surfaced in the suggestion line so a reviewer
# can copy-paste the ``spec team flag`` command without thinking.
_SEV_TO_FLAG_KIND = {
    SEV_INFO: "question",
    SEV_WARN: "warning",
    SEV_HIGH: "block",
}


@dataclass(frozen=True)
class Critique:
    """One suggestion the auto-critic surfaces against a prompt event.

    ``rule`` is the stable id (e.g. ``"destructive-verb"``) we display
    so engineers can ignore a noisy rule or look it up later. ``msg``
    is the human-readable hint. ``severity`` controls glyph + colour.
    ``suggested_flag_kind`` is what the reviewer would post if they
    decide to escalate the critique into a real :func:`spec team flag`.
    """

    rule: str
    severity: str
    msg: str
    suggested_flag_kind: str

    @property
    def glyph(self) -> str:
        if self.severity == SEV_HIGH:
            return "⛔"
        if self.severity == SEV_WARN:
            return "⚠"
        return "·"

    @property
    def color(self) -> str:
        if self.severity == SEV_HIGH:
            return "sf.reject"
        if self.severity == SEV_WARN:
            return "sf.warn"
        return "sf.muted"


# ── Regex catalogue ───────────────────────────────────────────────
#
# Compile once at import time. Each pattern is intentionally narrow:
# false positives erode trust in the critic faster than misses do.

# Destructive shell / SQL verbs. Boundary-anchored so we don't fire on
# "remove unused imports".
_DESTRUCTIVE = re.compile(
    r"""
    \b(
        rm\s+-rf?\b              # rm -r, rm -rf, rm -f
      | drop\s+(table|database|schema|index)\b
      | delete\s+from\b
      | truncate\s+table\b
      | git\s+reset\s+--hard\b
      | git\s+push\s+--force\b
      | git\s+push\s+-f\b
      | git\s+clean\s+-fd?\b
      | git\s+checkout\s+--\b
      | wipe\s+(the\s+)?(db|database|data|state)\b
      | format\s+(the\s+)?disk\b
      | sudo\s+rm\b
      | shutil\.rmtree\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Test-bypass language. The model is far too willing to oblige a
# "just delete the test" so this is a high-severity rule. We split
# the alternation into two patterns because some of the targets
# (``--no-verify``) start with a non-word character and so cannot
# share an outer ``\b`` anchor with the word-starting phrases.
_TEST_BYPASS_WORDS = re.compile(
    r"""
    \b(
        (disable|skip|comment\s+out|remove|delete|nuke)\s+
        (the\s+)?(failing\s+)?(unit\s+|integration\s+)?tests?\b
      | (disable|turn\s+off)\s+ci\b
      | skip\s+(the\s+)?pre[-\s]?commit\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_TEST_BYPASS_FLAGS = re.compile(
    r"(?<!\S)--no-verify\b",
    re.IGNORECASE,
)


def _matches_test_bypass(text: str) -> bool:
    return bool(_TEST_BYPASS_WORDS.search(text) or _TEST_BYPASS_FLAGS.search(text))

# Vague intent: a verb with no measurable acceptance criterion.
# We require that the verb is *the entire* core (no "fix the
# off-by-one in payments.py" — that has scope).
_VAGUE_VERBS = re.compile(
    r"""
    ^\s*
    (fix|improve|enhance|optimize|clean\s*up|refactor|tidy|polish)
    (\s+(this|that|it|the\s+code|things))?
    \s*[.!?]*\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Trust phrases — handing the wheel to the model without a destination.
_TRUST_HANDOFF = re.compile(
    r"""
    \b(
        (just\s+)?do\s+whatever\s+(you\s+(think|want))?
      | you\s+decide
      | (make|build)\s+(it|something)\s+(good|nice|cool|beautiful)
      | surprise\s+me
      | be\s+creative
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Multi-task conjunctions. We only fire when the prompt is also long
# enough to plausibly be two asks (>= 60 chars), otherwise short
# "do A and B" requests trip too often.
_MULTI_TASK = re.compile(r"\b(and\s+also|also\s+please|plus\s+also)\b", re.IGNORECASE)

# Likely secrets. Use coarse rules — false positives are not fatal,
# the suggestion just nudges a human review.
_SECRET_PATTERNS = re.compile(
    r"""
    (
        sk_(test|live)_[A-Za-z0-9]{16,}        # Stripe-style
      | xox[abp]-[A-Za-z0-9-]{10,}             # Slack
      | ghp_[A-Za-z0-9]{20,}                   # GitHub PAT
      | AKIA[0-9A-Z]{16}                       # AWS access key
      | -----BEGIN\s+(RSA|OPENSSH|DSA|EC|PGP)\s+PRIVATE\s+KEY-----
      | eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}
    )
    """,
    re.VERBOSE,
)


# ── Public API ────────────────────────────────────────────────────


def critique_event(event: IncomingEvent) -> list[Critique]:
    """Run every rule against ``event`` and return the firing critiques.

    Two role-specific rule sets:

    * **user turns** — full prompt-quality catalogue: vague intent,
      destructive verbs, test bypass, leaked secrets, trust handoff,
      multi-task.
    * **assistant turns** — narrower: we look for *blast radius* in
      the synthesized tool summary or in any prose the assistant did
      emit. Destructive verbs in a Bash command (``ran 1 tool: Bash
      "rm -rf node_modules"``) fire ``destructive-verb`` so the
      reviewer sees it *before* the tool actually lands.

    Presence rows are always skipped — they carry no inspectable text.
    """
    if event.role == "user":
        return _critique_user(event)
    if event.role == "assistant":
        return _critique_assistant(event)
    return []


def _critique_user(event: IncomingEvent) -> list[Critique]:
    body = (event.text or event.summary or "").strip()
    if not body:
        return []

    out: list[Critique] = []
    # Use the first 4 KB as the matching surface — covers ~99% of
    # real prompts and keeps regex cost predictable on accidental
    # giant pastes.
    head = body[:4096]

    if _DESTRUCTIVE.search(head):
        out.append(_critique(
            "destructive-verb",
            SEV_HIGH,
            "destructive operation requested — confirm with a human "
            "before letting the agent run",
        ))
    if _matches_test_bypass(head):
        out.append(_critique(
            "test-bypass",
            SEV_HIGH,
            "prompt asks to disable / skip / remove tests — almost "
            "always the wrong move; review the underlying failure",
        ))
    if _SECRET_PATTERNS.search(head):
        out.append(_critique(
            "secret-in-prompt",
            SEV_HIGH,
            "looks like a credential / private key was pasted into "
            "the prompt — rotate and redact",
        ))
    if _VAGUE_VERBS.search(head):
        out.append(_critique(
            "vague-intent",
            SEV_WARN,
            "vague verb with no scope — ask for a specific file / "
            "function / acceptance criterion",
        ))
    if _TRUST_HANDOFF.search(head):
        out.append(_critique(
            "trust-handoff",
            SEV_WARN,
            "open-ended delegation — the human has not actually "
            "decided what 'good' looks like",
        ))
    if len(head) >= 60 and _MULTI_TASK.search(head):
        out.append(_critique(
            "multi-task",
            SEV_INFO,
            "multiple asks in one prompt — splitting makes review "
            "and rollback cheaper",
        ))

    return out


def _critique_assistant(event: IncomingEvent) -> list[Critique]:
    """Inspect an assistant turn's summary + text for blast-radius
    warnings. The body we examine is the union of ``text`` (prose) and
    ``summary`` (which for tool-only turns carries the synthesized
    ``ran N tools: Bash "rm -rf …"`` line). Joining them lets a single
    regex pass catch the dangerous Bash command whether the
    broadcaster shared full text or only the summary."""
    parts: list[str] = []
    if isinstance(event.summary, str) and event.summary.strip():
        parts.append(event.summary.strip())
    if isinstance(event.text, str) and event.text.strip():
        parts.append(event.text.strip())
    if not parts:
        return []
    body = "\n".join(parts)[:4096]

    out: list[Critique] = []
    if _DESTRUCTIVE.search(body):
        out.append(_critique(
            "destructive-verb",
            SEV_HIGH,
            "AI is about to run a destructive operation — review the "
            "tool call before it lands",
        ))
    if _matches_test_bypass(body):
        out.append(_critique(
            "test-bypass",
            SEV_HIGH,
            "AI is about to disable / skip / remove tests — stop "
            "and review the underlying failure",
        ))
    if _SECRET_PATTERNS.search(body):
        out.append(_critique(
            "secret-in-output",
            SEV_HIGH,
            "AI output looks like it contains a credential — rotate "
            "and check for leakage",
        ))
    return out


def is_tool_only_summary(text: str | None) -> bool:
    """Cheap check: did the broadcaster mark this assistant turn as
    tool-only? ``True`` iff the summary starts with the sentinel
    prefix that ``watcher._synthesize_tool_summary`` writes. The
    receiver uses this to filter tool-only turns out of the live
    stream unless the critic has something to say about them.

    Keeping this function in ``critic.py`` (rather than depending on
    a constant from the broadcaster module) means the receiver does
    not need to import anything from ``realtime.watcher``."""
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    # Accept "ran 1 tool:" / "ran 12 tools:" but reject "ran out of
    # ideas" — must have a digit between ``ran`` and ``tool``.
    return bool(re.match(r"^ran\s+\d+\s+tools?:", stripped, re.IGNORECASE))


def _critique(rule: str, severity: str, msg: str) -> Critique:
    return Critique(
        rule=rule,
        severity=severity,
        msg=msg,
        suggested_flag_kind=_SEV_TO_FLAG_KIND[severity],
    )


def suggested_flag_command(event_id: int, c: Critique) -> str:
    """Render the exact CLI a reviewer would run to convert this
    auto-critique into a real, team-visible flag. Pre-filled with the
    rule name so the resulting flag's ``note`` carries provenance."""
    # Quote the note so spaces / quotes inside the message survive
    # paste-and-run without surprises.
    note = c.msg.replace('"', "'")
    return (
        f'spec team flag {event_id} '
        f'--kind {c.suggested_flag_kind} '
        f'--note "auto:{c.rule} — {note}"'
    )


__all__ = [
    "Critique",
    "SEV_INFO",
    "SEV_WARN",
    "SEV_HIGH",
    "critique_event",
    "is_tool_only_summary",
    "suggested_flag_command",
]


# Coerce ``Iterable[Critique]`` re-exports in callers that prefer to
# type the return value generically. Cheap alias.
CritiqueList = Iterable[Critique]
