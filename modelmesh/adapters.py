from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class AgentAdapter:
    key: str
    label: str
    command: list[str]
    description: str
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("AgentAdapter.key must be a non-empty string.")
        if not self.command:
            raise ValueError("AgentAdapter.command must not be empty.")


def is_available(adapter: AgentAdapter, command_override: list[str] | None = None) -> bool:
    """Return True if the agent binary is found on PATH."""
    cmd = command_override[0] if command_override else adapter.command[0]
    return shutil.which(cmd) is not None


def _runtime_dir(*parts: str) -> str:
    """Return a writable runtime path inside the current project."""
    return str(Path.cwd() / ".modelmesh-runtime" / Path(*parts))


def _xdg_env(agent_key: str) -> dict[str, str]:
    """Return writable XDG directories for agent CLIs that persist local state."""
    return {
        "XDG_CONFIG_HOME": _runtime_dir(agent_key, "xdg", "config"),
        "XDG_DATA_HOME": _runtime_dir(agent_key, "xdg", "data"),
        "XDG_STATE_HOME": _runtime_dir(agent_key, "xdg", "state"),
        "XDG_CACHE_HOME": _runtime_dir(agent_key, "xdg", "cache"),
    }


AGENT_REGISTRY: Final[dict[str, AgentAdapter]] = {
    "claude": AgentAdapter(
        key="claude",
        label="Claude Code",
        command=["claude"],
        description="Anthropic's Claude Code agent CLI.",
    ),
    "codex": AgentAdapter(
        key="codex",
        label="Codex",
        command=["codex"],
        description="OpenAI Codex agent CLI.",
        env={
            **_xdg_env("codex"),
            "HOME": _runtime_dir("codex", "home"),
        },
    ),
    "gemini": AgentAdapter(
        key="gemini",
        label="Gemini CLI",
        command=["gemini"],
        description="Google Gemini agent CLI.",
    ),
    "opencode": AgentAdapter(
        key="opencode",
        label="Open Code",
        command=["opencode"],
        description="Open-source coding agent CLI.",
        env=_xdg_env("opencode"),
    ),
    "openclaw": AgentAdapter(
        key="openclaw",
        label="OpenClaw",
        command=["openclaw"],
        description="OpenClaw coding agent CLI.",
    ),
}


def get_adapter(key: str, command_override: list[str] | None = None) -> AgentAdapter:
    """Return the AgentAdapter for *key*, optionally applying a command override.

    Raises KeyError for unknown keys.
    """
    adapter = AGENT_REGISTRY[key]  # raises KeyError if unknown
    if command_override is not None:
        adapter = AgentAdapter(
            key=adapter.key,
            label=adapter.label,
            command=command_override,
            description=adapter.description,
            env=adapter.env,
        )
    return adapter
