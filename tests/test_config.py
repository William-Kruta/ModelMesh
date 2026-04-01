"""Tests for modelmesh.config."""

from __future__ import annotations

from pathlib import Path

import pytest

import modelmesh.config as config_module
from modelmesh.config import AppConfig, load_config, save_config


def test_load_config_missing_file_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "config_path", lambda: tmp_path / "config.toml")
    result = load_config()
    assert result == AppConfig()


def test_save_and_load_config_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "config_path", lambda: tmp_path / "config.toml")
    original = AppConfig(
        active_agent="codex",
        project_root="/home/user/myproject",
        terminal_bottom_height=12,
        agent_overrides={"claude": ["claude", "--dangerously-skip-permissions"]},
    )
    save_config(original)
    loaded = load_config()
    assert loaded.active_agent == original.active_agent
    assert loaded.project_root == original.project_root
    assert loaded.terminal_bottom_height == original.terminal_bottom_height
    assert loaded.agent_overrides == original.agent_overrides


def test_load_config_malformed_toml_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("this is not valid toml ][[[", encoding="utf-8")
    monkeypatch.setattr(config_module, "config_path", lambda: config_file)
    result = load_config()
    assert result == AppConfig()


def test_load_config_newline_in_project_root_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "config_path", lambda: tmp_path / "config.toml")
    original = AppConfig(
        active_agent="claude",
        project_root="/home/user/my\nproject",
        agent_overrides={},
    )
    save_config(original)
    loaded = load_config()
    assert loaded.project_root == original.project_root


def test_load_config_clamps_terminal_bottom_height(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'active_agent = "claude"\nproject_root = ""\nterminal_bottom_height = 999\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "config_path", lambda: config_file)
    result = load_config()
    assert result.terminal_bottom_height == 18
