"""ModelMesh TUI — wires together adapters, sessions, and config."""

from __future__ import annotations
import threading
from pathlib import Path

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Header,
    Label,
    Static,
    TextArea,
)

from modelmesh.adapters import AGENT_REGISTRY, get_adapter, is_available
from modelmesh.config import AppConfig, config_path, load_config, save_config
from modelmesh.session import SessionManager, SessionState


# ---------------------------------------------------------------------------
# Session state → display mapping
# ---------------------------------------------------------------------------

_STATE_LABEL: dict[SessionState, str] = {
    SessionState.IDLE: "idle",
    SessionState.STARTING: "starting",
    SessionState.RUNNING: "running",
    SessionState.EXITED: "exited",
    SessionState.FAILED: "failed",
}

# ---------------------------------------------------------------------------
# Key map for TerminalPane
# ---------------------------------------------------------------------------

_KEY_MAP: dict[str, bytes] = {
    "enter": b"\r",
    "tab": b"\t",
    "backspace": b"\x7f",
    "escape": b"\x1b",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "delete": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
    "ctrl+a": b"\x01",
    "ctrl+b": b"\x02",
    "ctrl+c": b"\x03",
    "ctrl+d": b"\x04",
    "ctrl+e": b"\x05",
    "ctrl+f": b"\x06",
    "ctrl+g": b"\x07",
    "ctrl+h": b"\x08",
    "ctrl+i": b"\x09",
    "ctrl+j": b"\x0a",
    "ctrl+k": b"\x0b",
    "ctrl+l": b"\x0c",
    "ctrl+m": b"\x0d",
    "ctrl+n": b"\x0e",
    "ctrl+o": b"\x0f",
    "ctrl+p": b"\x10",
    "ctrl+q": b"\x11",
    "ctrl+r": b"\x12",
    "ctrl+s": b"\x13",
    "ctrl+t": b"\x14",
    "ctrl+u": b"\x15",
    "ctrl+v": b"\x16",
    "ctrl+w": b"\x17",
    "ctrl+x": b"\x18",
    "ctrl+y": b"\x19",
    "ctrl+z": b"\x1a",
    "ctrl+backslash": b"\x1c",
    "ctrl+right_square_bracket": b"\x1d",
}

_APP_RESERVED_KEYS = frozenset(
    {
        "ctrl+r",
        "ctrl+l",
        "ctrl+c",
        "f4",
        "f5",
        "f7",
        "f8",
        "f9",
        "f12",
    }
)

_APP_KEY_ACTIONS: dict[str, str] = {
    "ctrl+r": "pick_root",
    "ctrl+l": "clear_terminal",
    "ctrl+c": "quit",
    "f4": "expand_terminal",
    "f5": "shrink_terminal",
    "f7": "start_session",
    "f8": "stop_session",
    "f9": "restart_session",
    "f12": "open_config_editor",
}

_APP_CHARACTER_ACTIONS: dict[str, str] = {
    "\x03": "quit",
    "\x0c": "clear_terminal",
    "\x12": "pick_root",
}


# ---------------------------------------------------------------------------
# TerminalPane
# ---------------------------------------------------------------------------


class TerminalPane(ScrollableContainer):
    """Focusable terminal display widget that forwards key events to the active session."""

    can_focus = True
    BINDINGS = [
        Binding("shift+pageup", "scroll_page_up", "Scroll Up", show=False),
        Binding("shift+pagedown", "scroll_page_down", "Scroll Down", show=False),
        Binding("shift+home", "scroll_top", "Scroll Top", show=False),
        Binding("shift+end", "scroll_bottom", "Scroll Bottom", show=False),
    ]

    DEFAULT_CSS = """
    TerminalPane {
        height: 1fr;
        border: round #2e6b2b;
        padding: 1;
        background: #08110a;
        color: #8df08b;
        overflow: auto;
    }

    #terminal-body {
        width: auto;
        height: auto;
        min-height: 1;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._content: Text = Text()
        self._auto_follow = True
        self._mounted = False

    def compose(self) -> ComposeResult:
        yield Static(self._content, id="terminal-body")

    def on_mount(self) -> None:
        self._mounted = True
        self.call_after_refresh(self._resize_session_to_content_region)

    def update_content(self, text: Text) -> None:
        """Store new content and trigger a repaint."""
        self._content = text
        if self._mounted:
            self.query_one("#terminal-body", Static).update(text)
        if self._auto_follow:
            self.call_after_refresh(self._scroll_to_bottom)

    def on_key(self, event: events.Key) -> None:
        if event.key == "pageup":
            self.action_scroll_page_up()
            event.stop()
            return
        if event.key == "pagedown":
            self.action_scroll_page_down()
            event.stop()
            return
        if event.key == "home":
            self.action_scroll_top()
            event.stop()
            return
        if event.key == "end":
            self.action_scroll_bottom()
            event.stop()
            return

        character_action = (
            _APP_CHARACTER_ACTIONS.get(event.character) if event.character else None
        )
        if character_action is not None:
            self.app.run_action(character_action)
            event.stop()
            return

        if event.key in _APP_RESERVED_KEYS:
            action = _APP_KEY_ACTIONS.get(event.key)
            if action is not None:
                self.app.run_action(action)
                event.stop()
            return

        # Only forward if the active session is RUNNING
        session = self.app.session_manager.get(self.app.active_agent)
        if session is None or session.state != SessionState.RUNNING:
            return

        data = _KEY_MAP.get(event.key)
        if data is not None:
            self.app.forward_key_to_session(data)
            event.stop()
        elif event.character:
            self.app.forward_key_to_session(event.character.encode("utf-8"))
            event.stop()

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self._resize_session_to_content_region)

    def action_scroll_page_up(self) -> None:
        self._auto_follow = False
        self.scroll_relative(y=-(self.size.height or 1), animate=False, immediate=True)

    def action_scroll_page_down(self) -> None:
        self._auto_follow = False
        self.scroll_relative(y=self.size.height or 1, animate=False, immediate=True)

    def action_scroll_top(self) -> None:
        self._auto_follow = False
        self.scroll_to(y=0, animate=False, immediate=True)

    def action_scroll_bottom(self) -> None:
        self._auto_follow = True
        self.call_after_refresh(self._scroll_to_bottom)

    def follow_output(self) -> None:
        """Jump to the live bottom of the terminal output."""
        self._auto_follow = True
        self.call_after_refresh(self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        if self._auto_follow:
            self.scroll_end(animate=False, immediate=True, x_axis=False)

    def _resize_session_to_content_region(self) -> None:
        region = self.scrollable_content_region
        rows = max(1, region.height)
        cols = max(1, region.width)
        self.app.resize_active_session(rows, cols)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._auto_follow = False
        event.stop()
        self.scroll_relative(y=-3, animate=False, immediate=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self.scroll_relative(y=3, animate=False, immediate=True)
        if self.max_scroll_y - self.scroll_y <= 1:
            self._auto_follow = True


# ---------------------------------------------------------------------------
# ProjectRootScreen (directory picker — unchanged from Task 3)
# ---------------------------------------------------------------------------


class ProjectRootScreen(ModalScreen[Path | None]):
    """Directory picker for selecting the project root."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, initial_path: Path) -> None:
        super().__init__()
        self.initial_path = initial_path

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label("Select Project Root", id="picker-title")
            yield DirectoryTree(str(self.initial_path), id="project-tree")
            with Horizontal(id="picker-actions"):
                yield Button(
                    "Use Highlighted Directory", id="confirm-root", variant="primary"
                )
                yield Button("Cancel", id="cancel-root")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel-root")
    def cancel_button(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-root")
    def confirm_root(self) -> None:
        tree = self.query_one(DirectoryTree)
        node = tree.cursor_node
        if node is None:
            self.dismiss(None)
            return
        raw_path = getattr(node.data, "path", None)
        if raw_path is None:
            self.dismiss(None)
            return
        path = Path(raw_path)
        self.dismiss(path if path.is_dir() else path.parent)

    @on(DirectoryTree.DirectorySelected)
    def select_directory(self, event: DirectoryTree.DirectorySelected) -> None:
        self.dismiss(Path(event.path))

    @on(DirectoryTree.FileSelected)
    def select_file_parent(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(Path(event.path).parent)


# ---------------------------------------------------------------------------
# ConfigEditorScreen
# ---------------------------------------------------------------------------


class ConfigEditorScreen(ModalScreen[bool]):
    """Modal TOML config editor."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        path = config_path()
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        with Vertical(id="config-editor"):
            yield Label(f"Config: {path}", id="config-path-label")
            yield TextArea(text=raw, id="config-textarea")
            with Horizontal(id="config-actions"):
                yield Button("Save", id="config-save", variant="primary")
                yield Button("Cancel", id="config-cancel")

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#config-cancel")
    def cancel_button(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#config-save")
    def save_button(self) -> None:
        textarea = self.query_one("#config-textarea", TextArea)
        content = textarea.text
        path = config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception:  # noqa: BLE001
            self.dismiss(False)
            return
        self.dismiss(True)


# ---------------------------------------------------------------------------
# AgentSidebar
# ---------------------------------------------------------------------------


class AgentSidebar(Static):
    """Left-side agent selector with session state badges."""

    class AgentSelected(Message):
        def __init__(self, agent_key: str) -> None:
            self.agent_key = agent_key
            super().__init__()

    def compose(self) -> ComposeResult:
        yield Label("Agents", id="sidebar-title")
        for agent in AGENT_REGISTRY.values():
            with Horizontal(classes="agent-row"):
                yield Button(
                    agent.label, id=f"agent-{agent.key}", classes="agent-button"
                )
                yield Label(
                    "idle", id=f"badge-{agent.key}", classes="state-badge state-idle"
                )
        yield Button("Stop", id="sidebar-stop", variant="error")
        yield Button("Restart", id="sidebar-restart", variant="warning")

    def update_badge(self, agent_key: str, state: SessionState) -> None:
        """Update the state badge for *agent_key*."""
        try:
            badge = self.query_one(f"#badge-{agent_key}", Label)
        except Exception:  # noqa: BLE001
            return
        label_text = _STATE_LABEL.get(state, "unknown")
        badge.update(label_text)
        # Remove all existing state classes and apply the current one
        for s in SessionState:
            badge.remove_class(f"state-{s.value}")
        badge.add_class(f"state-{state.value}")

    @on(Button.Pressed, ".agent-button")
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id is None:
            return
        agent_key = event.button.id.removeprefix("agent-")
        self.post_message(self.AgentSelected(agent_key))


# ---------------------------------------------------------------------------
# ModelMeshApp
# ---------------------------------------------------------------------------


class ModelMeshApp(App[None]):
    """ModelMesh — terminal orchestrator for coding agent CLIs."""

    TITLE = "ModelMesh"
    SUB_TITLE = "One Workspace, Multiple Agents"

    CSS = """
    Screen {
        layout: horizontal;
        background: #061008;
        color: #8df08b;
    }

    Header {
        background: #102515;
        color: #b7ff9f;
        text-style: bold;
    }

    Footer {
        background: #102515;
        color: #7fe06f;
    }

    Button {
        background: #102515;
        color: #8df08b;
        border: round #2e6b2b;
    }

    Button:hover {
        background: #17351d;
        color: #caffb3;
    }

    Input, TextArea, DirectoryTree {
        background: #08110a;
        color: #8df08b;
        border: round #2e6b2b;
    }

    /* ---- Sidebar ---- */
    #sidebar {
        width: 34;
        padding: 1;
        border: thick #3f8a39;
        background: #0a160c;
    }

    #sidebar-title {
        text-style: bold;
        color: #d8ffb8;
        margin-bottom: 1;
    }

    .agent-row {
        height: auto;
        margin-bottom: 1;
    }

    .agent-button {
        width: 1fr;
    }

    .agent-button.-active {
        background: #214d24;
        color: #dfffbb;
        text-style: bold;
        border: thick #a5ff8f;
    }

    .state-badge {
        width: 12;
        height: 3;
        content-align: center middle;
        text-style: bold;
        padding: 0;
        margin-left: 1;
        background: #0d190f;
        border: round #244f22;
    }

    .state-idle    { color: #6e9c6b; }
    .state-starting { color: #f0d36d; }
    .state-running { color: #98ff7c; }
    .state-exited  { color: #50704f; }
    .state-failed  { color: #ff8f6b; }

    #sidebar-stop {
        width: 100%;
        margin-top: 1;
        background: #351512;
        color: #ffb19a;
        border: round #8a4035;
    }

    #sidebar-restart {
        width: 100%;
        margin-top: 1;
        background: #30280f;
        color: #f0d36d;
        border: round #8b7630;
    }

    /* ---- Main panel ---- */
    #main-panel {
        width: 1fr;
        padding: 0 1 1 1;
    }

    #status-bar {
        height: auto;
        border: round #2e6b2b;
        padding: 0 1;
        margin-bottom: 1;
        background: #0d190f;
        color: #b7ff9f;
        text-style: bold;
    }

    #terminal-display {
        margin: 0;
    }

    #system-scroll {
        height: 1fr;
    }

    #system-messages {
        height: 1fr;
        border: round #2e6b2b;
        padding: 0 1;
        color: #f0d36d;
        background: #0d190f;
    }

    #bottom-panel {
        height: 9;
        min-height: 5;
        margin-top: 1;
    }

    /* ---- Project root picker ---- */
    #picker {
        width: 80%;
        height: 80%;
        padding: 1;
        border: thick #3f8a39;
        background: #0a160c;
    }

    #picker-title {
        text-style: bold;
        margin-bottom: 1;
        color: #d8ffb8;
    }

    #project-tree {
        height: 1fr;
        border: round #2e6b2b;
        margin-bottom: 1;
    }

    #picker-actions {
        height: auto;
    }

    #confirm-root {
        margin-right: 1;
    }

    /* ---- Config editor ---- */
    #config-editor {
        width: 80%;
        height: 80%;
        padding: 1;
        border: thick #3f8a39;
        background: #0a160c;
    }

    #config-path-label {
        text-style: bold;
        color: #9ed597;
        margin-bottom: 1;
    }

    #config-textarea {
        height: 1fr;
        border: round #2e6b2b;
        margin-bottom: 1;
    }

    #config-actions {
        height: auto;
    }

    #config-save {
        margin-right: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+r", "pick_root", "Select Root", priority=True),
        Binding("ctrl+l", "clear_terminal", "Clear", priority=True),
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("f4", "expand_terminal", "More Terminal", priority=True),
        Binding("f5", "shrink_terminal", "Less Terminal", priority=True),
        Binding("f7", "start_session", "Start Agent", priority=True),
        Binding("f8", "stop_session", "Stop Agent", priority=True),
        Binding("f9", "restart_session", "Restart Agent", priority=True),
        Binding("f12", "open_config_editor", "Edit Config", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.session_manager = SessionManager()
        self._config: AppConfig = AppConfig()
        self.active_agent: str = next(iter(AGENT_REGISTRY))
        self.project_root: Path = Path.cwd()
        self._system_lines: list[str] = []
        self._bottom_panel_height = 9

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield AgentSidebar(id="sidebar")
        with Vertical(id="main-panel"):
            yield Static("", id="status-bar")
            yield TerminalPane(id="terminal-display")
            with Vertical(id="bottom-panel"):
                yield ScrollableContainer(
                    Static("", id="system-messages"),
                    id="system-scroll",
                )
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._config = load_config()
        # Restore persisted state
        if self._config.active_agent in AGENT_REGISTRY:
            self.active_agent = self._config.active_agent
        if self._config.project_root:
            candidate = Path(self._config.project_root)
            if candidate.is_dir():
                self.project_root = candidate
        self._bottom_panel_height = self._config.terminal_bottom_height

        self._refresh_agent_buttons()
        self._update_status()
        self._post_system("ModelMesh ready.")
        self._post_system(
            "Select an agent and a project root (Ctrl+R), then F7 to start."
        )
        self._apply_terminal_split()

        # PTY output triggers immediate UI sync; keep a light fallback poll so
        # cursor/input updates feel responsive even when a CLI redraw lags.
        self.set_interval(0.033, self._refresh_terminal)
        self.call_after_refresh(self._focus_terminal)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _active_label(self) -> str:
        return AGENT_REGISTRY[self.active_agent].label

    def _post_system(self, message: str) -> None:
        """Append a system message to the system-messages footer area."""
        try:
            widget = self.query_one("#system-messages", Static)
        except Exception:  # noqa: BLE001
            return
        self._system_lines.append(f"[system] {message}")
        self._system_lines = self._system_lines[-5:]
        widget.update("\n".join(self._system_lines))

    def _update_status(self) -> None:
        try:
            status = self.query_one("#status-bar", Static)
        except Exception:  # noqa: BLE001
            return
        session = self.session_manager.get(self.active_agent)
        state_str = session.state.value if session else "no session"
        status.update(
            f"Agent: [b]{self._active_label()}[/b]  "
            f"Root: [b]{self.project_root}[/b]  "
            f"Session: [b]{state_str}[/b]"
        )

    def _refresh_agent_buttons(self) -> None:
        for agent in AGENT_REGISTRY.values():
            try:
                button = self.query_one(f"#agent-{agent.key}", Button)
            except Exception:  # noqa: BLE001
                continue
            button.set_class(agent.key == self.active_agent, "-active")

    def _refresh_terminal(self) -> None:
        """Update terminal display from active session's pyte screen."""
        try:
            display = self.query_one("#terminal-display", TerminalPane)
        except Exception:  # noqa: BLE001
            return

        session = self.session_manager.get(self.active_agent)
        if session is None:
            return

        rich_text = session.get_screen_rich_lines()
        display.update_content(rich_text)

        # Also refresh badges for all agents
        self._refresh_all_badges()

    def _refresh_all_badges(self) -> None:
        """Update session state badges in the sidebar."""
        try:
            sidebar = self.query_one(AgentSidebar)
        except Exception:  # noqa: BLE001
            return
        for key in AGENT_REGISTRY:
            session = self.session_manager.get(key)
            state = session.state if session else SessionState.IDLE
            sidebar.update_badge(key, state)

    def _get_command_override(self, agent_key: str) -> list[str] | None:
        return self._config.agent_overrides.get(agent_key)

    def _ensure_session_started(self) -> None:
        """Auto-start the active agent session if it's IDLE."""
        cmd_override = self._get_command_override(self.active_agent)
        adapter = get_adapter(self.active_agent, cmd_override)
        session = self.session_manager.get_or_create(
            adapter, self.project_root, cmd_override
        )
        if session.state == SessionState.IDLE:
            if not is_available(adapter, cmd_override):
                self._post_system(
                    f"Agent binary for '{self.active_agent}' not found on PATH."
                )
                return
            try:
                session.set_output_callback(self._on_session_output)
                session.start()
                self._post_system(f"Started session for {self._active_label()}.")
            except RuntimeError as exc:
                self._post_system(f"Failed to start session: {exc}")

    def _on_session_output(self, _data: bytes) -> None:
        """Called from the reader thread when new PTY output arrives."""
        self.call_from_thread(self._sync_ui)

    def _sync_ui(self) -> None:
        """Refresh terminal display (called from thread)."""
        self._refresh_terminal()

    def _persist_config(self) -> None:
        """Save current active_agent and project_root to disk."""
        updated = AppConfig(
            active_agent=self.active_agent,
            project_root=str(self.project_root),
            terminal_bottom_height=self._bottom_panel_height,
            agent_overrides=self._config.agent_overrides,
        )
        save_config(updated)
        self._config = updated

    def _apply_terminal_split(self) -> None:
        try:
            bottom_panel = self.query_one("#bottom-panel", Vertical)
        except Exception:  # noqa: BLE001
            return
        bottom_panel.styles.height = self._bottom_panel_height
        bottom_panel.refresh(layout=True)

    def _focus_terminal(self) -> None:
        try:
            self.query_one("#terminal-display", TerminalPane).focus()
        except Exception:  # noqa: BLE001
            pass

    def forward_key_to_session(self, data: bytes) -> None:
        """Write raw bytes to the active session's PTY input."""
        session = self.session_manager.get(self.active_agent)
        if session is not None:
            session.write(data)
        try:
            self.query_one("#terminal-display", TerminalPane).follow_output()
        except Exception:  # noqa: BLE001
            pass

    def resize_active_session(self, rows: int, cols: int) -> None:
        """Resize the active session's PTY."""
        session = self.session_manager.get(self.active_agent)
        if session is not None:
            session.resize(rows, cols)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @on(AgentSidebar.AgentSelected)
    def handle_agent_selection(self, event: AgentSidebar.AgentSelected) -> None:
        if event.agent_key not in AGENT_REGISTRY:
            return
        self.active_agent = event.agent_key
        self._refresh_agent_buttons()
        self._update_status()
        self._post_system(f"Switched to {self._active_label()}.")
        self._persist_config()
        # Clear the terminal display so we see the correct session's screen
        try:
            display = self.query_one("#terminal-display", TerminalPane)
            display.update_content(Text(""))
        except Exception:  # noqa: BLE001
            pass

        self.call_after_refresh(self._focus_terminal)

    @on(Button.Pressed, "#sidebar-stop")
    def sidebar_stop_pressed(self) -> None:
        self.action_stop_session()

    @on(Button.Pressed, "#sidebar-restart")
    def sidebar_restart_pressed(self) -> None:
        self.action_restart_session()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_pick_root(self) -> None:
        self.push_screen(
            ProjectRootScreen(self.project_root), self._handle_root_selection
        )

    def _handle_root_selection(self, selected_path: Path | None) -> None:
        if selected_path is None:
            self._post_system("Project root selection canceled.")
            return
        self.project_root = selected_path.resolve()
        self._update_status()
        self._post_system(f"Project root set to {self.project_root}.")
        self._persist_config()

    def action_clear_terminal(self) -> None:
        try:
            display = self.query_one("#terminal-display", TerminalPane)
            display.update_content(Text(""))
        except Exception:  # noqa: BLE001
            pass
        self._post_system("Terminal display cleared.")

    def action_start_session(self) -> None:
        self._ensure_session_started()
        self._update_status()
        self.call_after_refresh(self._focus_terminal)

    def action_stop_session(self) -> None:
        session = self.session_manager.get(self.active_agent)
        if session is None:
            self._post_system("No session to stop.")
            return
        agent_key = self.active_agent
        agent_label = self._active_label()
        self._post_system(f"Stopping session for {agent_label}...")
        threading.Thread(
            target=self._stop_session_in_background,
            args=(agent_key, agent_label),
            name=f"stop-session-{agent_key}",
            daemon=True,
        ).start()

    def action_restart_session(self) -> None:
        cmd_override = self._get_command_override(self.active_agent)
        adapter = get_adapter(self.active_agent, cmd_override)
        session = self.session_manager.get_or_create(
            adapter, self.project_root, cmd_override
        )
        if not is_available(adapter, cmd_override):
            self._post_system(
                f"Agent binary for '{self.active_agent}' not found on PATH."
            )
            return
        agent_key = self.active_agent
        agent_label = self._active_label()
        self._post_system(f"Restarting session for {agent_label}...")
        threading.Thread(
            target=self._restart_session_in_background,
            args=(agent_key, agent_label),
            name=f"restart-session-{agent_key}",
            daemon=True,
        ).start()

    def action_open_config_editor(self) -> None:
        self.push_screen(ConfigEditorScreen(), self._handle_config_saved)

    def action_expand_terminal(self) -> None:
        self._bottom_panel_height = max(5, self._bottom_panel_height - 1)
        self._apply_terminal_split()
        self._post_system("Terminal made taller (F4/F5 to adjust).")

    def action_shrink_terminal(self) -> None:
        self._bottom_panel_height = min(18, self._bottom_panel_height + 1)
        self._apply_terminal_split()
        self._post_system("Terminal made shorter (F4/F5 to adjust).")

    def _handle_config_saved(self, saved: bool) -> None:
        if not saved:
            self._post_system("Config editor closed without saving.")
            return
        # Reload config from disk
        self._config = load_config()
        if self._config.active_agent in AGENT_REGISTRY:
            self.active_agent = self._config.active_agent
        if self._config.project_root:
            candidate = Path(self._config.project_root)
            if candidate.is_dir():
                self.project_root = candidate
        self._refresh_agent_buttons()
        self._update_status()
        self._post_system("Config saved and reloaded.")

    def action_quit(self) -> None:
        self.session_manager.stop_all()
        self.exit()

    def _stop_session_in_background(self, agent_key: str, agent_label: str) -> None:
        session = self.session_manager.get(agent_key)
        if session is None:
            self.call_from_thread(self._post_system, "No session to stop.")
            return
        try:
            session.stop()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._post_system, f"Error stopping session: {exc}")
        else:
            self.call_from_thread(
                self._post_system, f"Stopped session for {agent_label}."
            )
        self.call_from_thread(self._sync_ui)
        self.call_from_thread(self._update_status)
        self.call_from_thread(self._refresh_all_badges)

    def _restart_session_in_background(self, agent_key: str, agent_label: str) -> None:
        cmd_override = self._get_command_override(agent_key)
        adapter = get_adapter(agent_key, cmd_override)
        session = self.session_manager.get_or_create(
            adapter, self.project_root, cmd_override
        )
        try:
            session.set_output_callback(self._on_session_output)
            session.restart()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self._post_system, f"Error restarting session: {exc}"
            )
        else:
            self.call_from_thread(
                self._post_system, f"Restarted session for {agent_label}."
            )
        self.call_from_thread(self._sync_ui)
        self.call_from_thread(self._update_status)
        self.call_from_thread(self._refresh_all_badges)
