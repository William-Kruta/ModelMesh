"""PTY-backed subprocess session manager for ModelMesh agents."""

from __future__ import annotations

import enum
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyte
import ptyprocess
from rich.style import Style
from rich.text import Text

from modelmesh.adapters import AgentAdapter, is_available

# Default terminal dimensions
_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24
_DEFAULT_SCROLLBACK = 2000

# Named colors recognized by pyte (without "bright" prefix)
_PYTE_NAMED_COLORS = frozenset(
    {"black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"}
)
_OPENCODE_DARK_ACCENT_COLORS = frozenset({"#0a0a0a", "#1e1e1e", "#282828", "#434343"})

_CSI_CURSOR_POSITION = b"\x1b[6n"
_CSI_PRIMARY_DEVICE_ATTRIBUTES = b"\x1b[c"
_CSI_KITTY_KEYBOARD_QUERY = b"\x1b[?u"


def _pyte_color_to_rich(color: str | int) -> str | None:
    """Convert a pyte color value to a Rich-compatible color string.

    Args:
        color: A pyte color value — ``"default"``, a named color string,
               an integer 0-255 (256-color palette), or a 6-char hex string.

    Returns:
        A Rich color string, or ``None`` if no color should be applied.
    """
    if isinstance(color, int):
        return f"color({color})"

    # Normalise to lowercase string for comparison
    s = color.lower()

    if s in ("default", ""):
        return None

    if s in _PYTE_NAMED_COLORS:
        return s

    if s.startswith("bright"):
        base = s[len("bright"):]
        if base in _PYTE_NAMED_COLORS:
            return f"bright_{base}"

    # 6-char hex → Rich 24-bit color
    if len(s) == 6 and all(c in "0123456789abcdef" for c in s):
        return f"#{s}"

    return None


def _row_has_visible_content(row_data: dict[int, Any], columns: int) -> bool:
    """Return True if a screen row contains any non-whitespace character."""
    for col in range(columns):
        char = row_data.get(col)
        if char is not None and char.data and not char.data.isspace():
            return True
    return False


class SessionState(enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"


class TerminalScreen(pyte.HistoryScreen):
    """pyte screen with compatibility shims for modern CLI TUIs."""

    def __init__(self, columns: int, lines: int) -> None:
        super().__init__(columns, lines, history=_DEFAULT_SCROLLBACK)

    def report_device_status(self, mode: int, private: bool = False) -> None:
        """Ignore private DSR queries that upstream pyte can't dispatch."""
        if private:
            return
        super().report_device_status(mode)


class AgentSession:
    """Manages a single PTY subprocess for one agent."""

    def __init__(
        self,
        adapter: AgentAdapter,
        project_root: Path,
        command_override: list[str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._project_root = project_root
        self._command_override = command_override

        self._state: SessionState = SessionState.IDLE
        self._lock = threading.Lock()
        self._stop_requested = False

        self._process: ptyprocess.PtyProcess | None = None
        self._reader_thread: threading.Thread | None = None
        self._output_callback: Callable[[bytes], None] | None = None

        # Terminal emulation
        self._cols = _DEFAULT_COLS
        self._rows = _DEFAULT_ROWS
        self._screen = TerminalScreen(self._cols, self._rows)
        self._stream = pyte.ByteStream(self._screen)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def adapter(self) -> AgentAdapter:
        return self._adapter

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the agent process in a PTY inside project_root.

        Raises RuntimeError if the binary is unavailable or the session is
        already starting/running.
        """
        with self._lock:
            if self._state in (SessionState.STARTING, SessionState.RUNNING):
                raise RuntimeError(
                    f"Session for '{self._adapter.key}' is already {self._state.value}."
                )
            if not is_available(self._adapter, self._command_override):
                raise RuntimeError(
                    f"Agent binary for '{self._adapter.key}' not found on PATH."
                )
            self._state = SessionState.STARTING
            self._stop_requested = False

        command = self._command_override or self._adapter.command
        env = {**os.environ, **self._adapter.env}

        for key in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ):
            path_value = env.get(key)
            if path_value:
                Path(path_value).mkdir(parents=True, exist_ok=True)

        try:
            process = ptyprocess.PtyProcess.spawn(
                command,
                cwd=str(self._project_root),
                env=env,
                dimensions=(self._rows, self._cols),
            )
        except Exception as exc:
            with self._lock:
                self._state = SessionState.FAILED
            raise RuntimeError(
                f"Failed to spawn '{self._adapter.key}': {exc}"
            ) from exc

        with self._lock:
            self._process = process
            self._state = SessionState.RUNNING

        # Start background reader thread
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(process,),
            name=f"session-reader-{self._adapter.key}",
            daemon=True,
        )
        self._reader_thread.start()

    def stop(self) -> None:
        """Send SIGTERM; escalate to SIGKILL after 2 seconds if still alive."""
        with self._lock:
            process = self._process
            reader_thread = self._reader_thread
            if process is None or not process.isalive():
                return
            self._stop_requested = True

        try:
            process.kill(signal.SIGTERM)
        except OSError:
            return

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not process.isalive():
                break
            time.sleep(0.05)

        if process.isalive():
            try:
                process.kill(signal.SIGKILL)
            except OSError:
                pass

        # Wait for the reader thread to finish so state is finalized before we return.
        if reader_thread is not None and reader_thread.is_alive():
            reader_thread.join(timeout=5.0)

    def restart(self) -> None:
        """Stop (if running) then start a fresh session."""
        # stop() now joins the reader thread, so by the time it returns the
        # state is finalized and we can safely reset it.
        self.stop()
        with self._lock:
            self._process = None
            self._reader_thread = None
            self._state = SessionState.IDLE
            self._stop_requested = False
        self.start()

    def write(self, data: bytes) -> None:
        """Write bytes to the PTY (forwards keystrokes to the agent).

        Silently no-ops if the session is not running.
        """
        with self._lock:
            if self._state != SessionState.RUNNING:
                return
            process = self._process

        if process is not None:
            try:
                process.write(data)
            except OSError as exc:
                logging.warning("write() failed for '%s': %s", self._adapter.key, exc)

    def set_output_callback(self, callback: Callable[[bytes], None]) -> None:
        """Register a callback invoked with raw PTY output bytes as they arrive."""
        with self._lock:
            self._output_callback = callback

    def resize(self, rows: int, cols: int) -> None:
        """Resize the PTY window and the internal pyte screen."""
        with self._lock:
            self._rows = rows
            self._cols = cols
            self._screen.resize(rows, cols)
            process = self._process

        if process is not None and process.isalive():
            try:
                process.setwinsize(rows, cols)
            except OSError as exc:
                logging.warning("resize() failed for '%s': %s", self._adapter.key, exc)

    def get_screen_lines(self) -> list[str]:
        """Return the current terminal screen as a list of strings (one per row)."""
        with self._lock:
            return [self._screen.display[row] for row in range(self._screen.lines)]

    def get_screen_rich_lines(self) -> Text:
        """Return current terminal screen as Rich Text with colors and styles."""
        with self._lock:
            screen = self._screen
            buffer = dict(screen.buffer)
            lines = self._screen.lines
            columns = self._screen.columns
            cursor_row = self._screen.cursor.y
            strip_backgrounds = self._adapter.key == "opencode"
            history_rows = list(screen.history.top)

        current_rows: list[dict[int, Any]] = [buffer.get(row, {}) for row in range(lines)]

        # Only render the current viewport up to the last row that has visible
        # content or the cursor row. Historical rows are always preserved.
        last_content_row = cursor_row
        for row in range(lines):
            row_data = current_rows[row]
            if _row_has_visible_content(row_data, columns):
                last_content_row = row

        visible_current_rows = current_rows[: last_content_row + 1]
        if self._adapter.key == "opencode" and visible_current_rows:
            while visible_current_rows and not _row_has_visible_content(
                visible_current_rows[0], columns
            ):
                visible_current_rows.pop(0)
            while visible_current_rows and not _row_has_visible_content(
                visible_current_rows[-1], columns
            ):
                visible_current_rows.pop()
        all_rows = history_rows + visible_current_rows

        result = Text()
        history_count = len(history_rows)
        for row_index, row_data in enumerate(all_rows):
            last_render_col = -1
            for col in range(columns):
                char = row_data.get(col)
                if char is not None and char.data and not char.data.isspace():
                    last_render_col = col

            if row_index == history_count + cursor_row:
                last_render_col = max(last_render_col, self._screen.cursor.x)

            if last_render_col < 0:
                if row_index < len(all_rows) - 1:
                    result.append("\n")
                continue

            for col in range(last_render_col + 1):
                char = row_data.get(col)
                if char is None:
                    char_data = " "
                    fg = None
                    bg = None
                    bold = None
                    italic = None
                    underline = None
                    reverse = None
                else:
                    char_data = char.data if char.data else " "
                    fg = _pyte_color_to_rich(char.fg)
                    bg = _pyte_color_to_rich(char.bg)
                    bold = char.bold or None
                    italic = char.italics or None
                    underline = char.underscore or None
                    reverse = char.reverse or None

                # Terminal UIs often draw padding with styled spaces. Rendering
                # underline / italic / reverse on blank cells produces noisy
                # horizontal rules and underlined gaps that aren't present in
                # the source UI, so keep spaces visually neutral.
                if char_data.isspace():
                    fg = None
                    bg = None
                    bold = None
                    italic = None
                    underline = None
                    reverse = None
                elif strip_backgrounds:
                    bg = None
                    underline = None
                    reverse = None
                    if fg in _OPENCODE_DARK_ACCENT_COLORS:
                        fg = None

                style = Style(
                    color=fg,
                    bgcolor=bg,
                    bold=bold,
                    italic=italic,
                    underline=underline,
                    reverse=reverse,
                )
                result.append(char_data, style=style)

            if row_index < len(all_rows) - 1:
                result.append("\n")

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_terminal_query_responses(self, data: bytes) -> list[bytes]:
        """Return terminal capability replies required by interactive CLIs."""
        responses: list[bytes] = []
        if _CSI_CURSOR_POSITION in data:
            with self._lock:
                row = self._screen.cursor.y + 1
                col = self._screen.cursor.x + 1
            responses.append(f"\x1b[{row};{col}R".encode())
        if _CSI_PRIMARY_DEVICE_ATTRIBUTES in data:
            responses.append(b"\x1b[?62;c")
        if _CSI_KITTY_KEYBOARD_QUERY in data:
            responses.append(b"\x1b[?0u")
        return responses

    def _respond_to_terminal_queries(
        self, process: ptyprocess.PtyProcess, data: bytes
    ) -> None:
        """Reply to terminal feature probes emitted by the child process."""
        for response in self._build_terminal_query_responses(data):
            try:
                process.write(response)
            except OSError as exc:
                logging.warning(
                    "terminal query response failed for '%s': %s",
                    self._adapter.key,
                    exc,
                )
                break

    def _reader_loop(self, process: ptyprocess.PtyProcess) -> None:
        """Background thread: read PTY output and feed it to pyte + callback."""
        while True:
            try:
                data = process.read(4096)
            except EOFError:
                break
            except OSError as exc:
                logging.warning("_reader_loop read error for '%s': %s", self._adapter.key, exc)
                break

            if not data:
                break

            self._respond_to_terminal_queries(process, data)

            with self._lock:
                self._stream.feed(data)
                callback = self._output_callback

            if callback is not None:
                try:
                    callback(data)
                except Exception as exc:  # noqa: BLE001
                    logging.warning(
                        "_reader_loop callback error for '%s': %s", self._adapter.key, exc
                    )

        # Process has ended — determine exit state
        exit_code: int | None = None
        try:
            # Give the process a moment to report its status
            for _ in range(20):
                if not process.isalive():
                    break
                time.sleep(0.05)
            exit_code = process.exitstatus
        except Exception as exc:  # noqa: BLE001
            logging.warning("_reader_loop exitstatus error for '%s': %s", self._adapter.key, exc)

        with self._lock:
            if exit_code == 0:
                self._state = SessionState.EXITED
            elif self._stop_requested:
                self._state = SessionState.EXITED
            else:
                self._state = SessionState.FAILED


class SessionManager:
    """Manages one AgentSession per agent key."""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        adapter: AgentAdapter,
        project_root: Path,
        command_override: list[str] | None = None,
    ) -> AgentSession:
        """Return existing session for adapter.key, or create a new one."""
        with self._lock:
            session = self._sessions.get(adapter.key)
            if session is None:
                session = AgentSession(adapter, project_root, command_override)
                self._sessions[adapter.key] = session
        return session

    def get(self, key: str) -> AgentSession | None:
        """Return the session for *key*, or None if it doesn't exist."""
        with self._lock:
            return self._sessions.get(key)

    def stop_all(self) -> None:
        """Stop all running sessions."""
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.stop()
