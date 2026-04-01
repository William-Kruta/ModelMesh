"""Tests for modelmesh.session (no real agent processes spawned)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from modelmesh.adapters import AgentAdapter
from modelmesh.session import AgentSession, SessionManager, SessionState


def _make_adapter(binary: str = "nonexistent_binary_xyz_abc") -> AgentAdapter:
    return AgentAdapter(
        key="test-agent",
        label="Test Agent",
        command=[binary],
        description="Adapter used in tests.",
    )


def test_session_manager_can_be_created() -> None:
    manager = SessionManager()
    assert manager is not None


def test_get_or_create_returns_agent_session(tmp_path: Path) -> None:
    manager = SessionManager()
    adapter = _make_adapter()
    session = manager.get_or_create(adapter, tmp_path)
    assert isinstance(session, AgentSession)


def test_new_session_state_is_idle(tmp_path: Path) -> None:
    manager = SessionManager()
    adapter = _make_adapter()
    session = manager.get_or_create(adapter, tmp_path)
    assert session.state == SessionState.IDLE


def test_start_with_unavailable_binary_raises_runtime_error(tmp_path: Path) -> None:
    adapter = _make_adapter(binary="nonexistent_binary_xyz_abc")
    session = AgentSession(adapter, tmp_path)
    with pytest.raises(RuntimeError):
        session.start()


def test_write_when_idle_does_not_raise(tmp_path: Path) -> None:
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)
    # State is IDLE — write should silently no-op
    session.write(b"hello")


def test_set_output_callback_does_not_raise(tmp_path: Path) -> None:
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)
    session.set_output_callback(lambda data: None)


def test_get_screen_lines_returns_list_of_strings_when_idle(tmp_path: Path) -> None:
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)
    lines = session.get_screen_lines()
    assert isinstance(lines, list)
    assert all(isinstance(line, str) for line in lines)


def test_get_screen_rich_lines_returns_text_when_idle(tmp_path: Path) -> None:
    from rich.text import Text
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)
    result = session.get_screen_rich_lines()
    assert isinstance(result, Text)


def test_pyte_color_to_rich_conversions() -> None:
    from modelmesh.session import _pyte_color_to_rich
    assert _pyte_color_to_rich("default") is None
    assert _pyte_color_to_rich("") is None
    assert _pyte_color_to_rich("red") == "red"
    assert _pyte_color_to_rich("brightred") == "bright_red"
    assert _pyte_color_to_rich(196) == "color(196)"
    assert _pyte_color_to_rich("ff8800") == "#ff8800"


def test_blank_cells_do_not_render_underline_styles(tmp_path: Path) -> None:
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)

    session._stream.feed(b"\x1b[4m \x1b[0mX")

    result = session.get_screen_rich_lines()

    assert result.plain.startswith(" X")
    assert not any(
        span.start == 0 and span.end == 1 and getattr(span.style, "underline", False)
        for span in result.spans
    )


def test_trailing_whitespace_is_trimmed_from_rendered_rows(tmp_path: Path) -> None:
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)

    session._stream.feed(b"abc")

    result = session.get_screen_rich_lines()

    assert result.plain.rstrip() == "abc"
    assert len(result.plain) <= 4


def test_private_device_status_sequence_does_not_crash(tmp_path: Path) -> None:
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)

    session._stream.feed(b"\x1b[?2026nhello")

    result = session.get_screen_rich_lines()

    assert "hello" in result.plain


def test_stop_marks_user_terminated_process_as_exited(tmp_path: Path) -> None:
    adapter = AgentAdapter(
        key="sleepy",
        label="Sleepy",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        description="Long-running test process.",
    )
    session = AgentSession(adapter, tmp_path)

    session.start()
    assert session.state == SessionState.RUNNING

    session.stop()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and session.state == SessionState.RUNNING:
        time.sleep(0.05)

    assert session.state == SessionState.EXITED


def test_terminal_query_responses_include_cursor_and_capabilities(tmp_path: Path) -> None:
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)
    session._screen.cursor.x = 4
    session._screen.cursor.y = 2

    responses = session._build_terminal_query_responses(
        b"\x1b[6n\x1b[c\x1b[?u"
    )

    assert responses == [b"\x1b[3;5R", b"\x1b[?62;c", b"\x1b[?0u"]


def test_scrollback_history_is_included_in_rendered_output(tmp_path: Path) -> None:
    adapter = _make_adapter()
    session = AgentSession(adapter, tmp_path)
    session.resize(3, 20)

    for line in ("one\n", "two\n", "three\n", "four\n"):
        session._stream.feed(line.encode())

    result = session.get_screen_rich_lines()

    assert "one" in result.plain
    assert "four" in result.plain


def test_opencode_rendering_strips_background_colors(tmp_path: Path) -> None:
    adapter = AgentAdapter(
        key="opencode",
        label="Open Code",
        command=["fake-opencode"],
        description="Adapter used in tests.",
    )
    session = AgentSession(adapter, tmp_path)

    session._stream.feed(b"\x1b[48;5;235mX\x1b[0m")

    result = session.get_screen_rich_lines()

    assert result.plain.startswith("X")
    assert all(span.style.bgcolor is None for span in result.spans)


def test_opencode_rendering_strips_dark_accent_foregrounds(tmp_path: Path) -> None:
    adapter = AgentAdapter(
        key="opencode",
        label="Open Code",
        command=["fake-opencode"],
        description="Adapter used in tests.",
    )
    session = AgentSession(adapter, tmp_path)

    session._stream.feed(b"\x1b[38;2;30;30;30mX\x1b[0m")

    result = session.get_screen_rich_lines()

    assert result.plain.startswith("X")
    assert all(span.style.color is None for span in result.spans)
