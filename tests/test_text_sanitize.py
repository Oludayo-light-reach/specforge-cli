import tomllib

from spec_cli.prompts.render import render_prompts_file
from spec_cli.prompts.schema import CommitMeta, PromptsFile, Session, Turn
from spec_cli.prompts.text_sanitize import (
    CURSOR_REDACTED_PLACEHOLDER,
    is_cursor_redacted_placeholder,
    prose_without_redacted_placeholders,
    sanitize_for_toml_text,
    strip_ansi_escapes,
    unwrap_cursor_user_message,
)


def test_unwrap_cursor_user_message_strips_envelope():
    raw = (
        "<timestamp>Friday, May 15, 2026, 8:02 PM (UTC+8)</timestamp>\n"
        "<user_query>\n"
        "hi\n"
        "</user_query>"
    )
    assert unwrap_cursor_user_message(raw) == "hi"


def test_is_cursor_redacted_placeholder():
    assert is_cursor_redacted_placeholder(CURSOR_REDACTED_PLACEHOLDER)
    assert is_cursor_redacted_placeholder(f"{CURSOR_REDACTED_PLACEHOLDER}\n{CURSOR_REDACTED_PLACEHOLDER}")
    assert not is_cursor_redacted_placeholder("On it.\n[REDACTED]")
    assert not is_cursor_redacted_placeholder("hello")


def test_prose_without_redacted_placeholders():
    assert prose_without_redacted_placeholders("On it.\n[REDACTED]") == "On it."


def test_unwrap_cursor_user_message_plain_text_unchanged():
    assert unwrap_cursor_user_message("Refactor billing.py please.") == (
        "Refactor billing.py please."
    )


def test_strip_ansi_sgr():
    s = "hello \x1b[31mred\x1b[0m world"
    out = strip_ansi_escapes(s)
    assert "red" in out
    assert "\x1b" not in out


def test_sanitize_drops_c0_besides_whitespace():
    s = "a\x07b\nc"
    t = sanitize_for_toml_text(s)
    assert "\x07" not in t
    assert "\n" in t


def test_sanitize_for_toml_strips_ansi_for_multiline():
    s = "x\n\x1b[1mlong\x1b[0m\ny"
    t = sanitize_for_toml_text(s)
    assert "\x1b" not in t
    assert "long" in t


def test_render_long_user_text_with_ansi_produces_parseable_toml():
    bad = "line one\n\x1b[2J\x1b[0;0Hline two"
    pf = PromptsFile(
        commit=CommitMeta(
            branch="main",
            author_name="a",
            author_email="a@a.com",
        ),
        sessions=[
            Session(
                id="one",
                source="manual",
                turns=[Turn(role="user", text=bad)],
            )
        ],
    )
    body = render_prompts_file(pf)
    assert "\x1b" not in body
    tomllib.loads(body)
