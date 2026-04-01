"""Smoke tests for modelmesh.app — import and construction only, no event loop."""

from __future__ import annotations

import asyncio

from modelmesh.app import ConfigEditorScreen, ModelMeshApp


def test_import() -> None:
    assert ModelMeshApp is not None
    assert ConfigEditorScreen is not None


def test_construct() -> None:
    app = ModelMeshApp()
    assert app is not None


def test_terminal_pane_importable() -> None:
    from modelmesh.app import TerminalPane
    assert TerminalPane is not None
    assert TerminalPane.BINDINGS


def test_key_map_has_required_entries() -> None:
    from modelmesh.app import (
        _APP_CHARACTER_ACTIONS,
        _APP_KEY_ACTIONS,
        _APP_RESERVED_KEYS,
        _KEY_MAP,
    )
    assert _KEY_MAP["enter"] == b"\r"
    assert _KEY_MAP["up"] == b"\x1b[A"
    assert _KEY_MAP["ctrl+c"] == b"\x03"
    assert _KEY_MAP["escape"] == b"\x1b"
    assert len(_KEY_MAP) >= 40
    assert "f4" in _APP_RESERVED_KEYS
    assert "f5" in _APP_RESERVED_KEYS
    assert "f8" in _APP_RESERVED_KEYS
    assert "f12" in _APP_RESERVED_KEYS
    assert _APP_KEY_ACTIONS["f4"] == "expand_terminal"
    assert _APP_KEY_ACTIONS["f5"] == "shrink_terminal"
    assert _APP_KEY_ACTIONS["f8"] == "stop_session"
    assert _APP_KEY_ACTIONS["f12"] == "open_config_editor"
    assert "\x05" not in _APP_CHARACTER_ACTIONS
    assert "\x0b" not in _APP_CHARACTER_ACTIONS


def test_app_mounts_with_pilot() -> None:
    async def run() -> None:
        app = ModelMeshApp()
        async with app.run_test():
            assert app.query_one("#terminal-display") is not None
            assert app.query_one("#sidebar") is not None
            assert app.query_one("#status-bar") is not None

    asyncio.run(run())
