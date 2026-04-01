"""Tests for modelmesh.adapters."""

from __future__ import annotations

import pytest

from modelmesh.adapters import AGENT_REGISTRY, AgentAdapter, get_adapter, is_available


def test_all_five_agent_keys_present() -> None:
    expected = {"claude", "codex", "gemini", "opencode", "openclaw"}
    assert expected == set(AGENT_REGISTRY.keys())


def test_get_adapter_returns_correct_key() -> None:
    adapter = get_adapter("claude")
    assert adapter.key == "claude"


def test_get_adapter_unknown_key_raises_key_error() -> None:
    with pytest.raises(KeyError):
        get_adapter("unknown_key")


def test_get_adapter_command_override() -> None:
    adapter = get_adapter("claude", command_override=["my-claude"])
    assert adapter.command == ["my-claude"]


def test_is_available_true_for_python_binary() -> None:
    # python3 or python is definitely on PATH
    for binary in ("python3", "python"):
        try:
            fake_adapter = AgentAdapter(
                key="test-python",
                label="Test Python",
                command=[binary],
                description="Fake adapter using python binary.",
            )
            result = is_available(fake_adapter)
            assert result is True
            return  # at least one of python3/python found
        except Exception:
            continue
    pytest.fail("Neither python3 nor python was found on PATH")


def test_is_available_false_for_nonexistent_binary() -> None:
    fake_adapter = AgentAdapter(
        key="fake-nonexistent",
        label="Fake Nonexistent",
        command=["nonexistent_binary_xyz_abc"],
        description="Fake adapter with a binary that does not exist.",
    )
    assert is_available(fake_adapter) is False


def test_agent_adapter_empty_command_raises_value_error() -> None:
    with pytest.raises(ValueError):
        AgentAdapter(
            key="test",
            label="Test",
            command=[],
            description="Should fail.",
        )


def test_agent_adapter_empty_key_raises_value_error() -> None:
    with pytest.raises(ValueError):
        AgentAdapter(
            key="",
            label="Test",
            command=["some-binary"],
            description="Should fail.",
        )


def test_codex_adapter_uses_writable_runtime_dirs() -> None:
    adapter = AGENT_REGISTRY["codex"]
    assert adapter.env["HOME"].endswith(".modelmesh-runtime/codex/home")
    assert adapter.env["XDG_DATA_HOME"].endswith(".modelmesh-runtime/codex/xdg/data")


def test_opencode_adapter_uses_writable_xdg_dirs() -> None:
    adapter = AGENT_REGISTRY["opencode"]
    assert "HOME" not in adapter.env
    assert adapter.env["XDG_STATE_HOME"].endswith(".modelmesh-runtime/opencode/xdg/state")
