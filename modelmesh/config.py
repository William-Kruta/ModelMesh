"""Config loader/writer for ModelMesh using ~/.config/modelmesh/config.toml."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Config file location
# ---------------------------------------------------------------------------

def config_path() -> Path:
    """Return the path to config.toml."""
    try:
        from platformdirs import user_config_dir
        base = Path(user_config_dir("modelmesh"))
    except ImportError:
        base = Path.home() / ".config" / "modelmesh"
    return base / "config.toml"


# ---------------------------------------------------------------------------
# AppConfig dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    active_agent: str = "claude"
    project_root: str = ""
    terminal_bottom_height: int = 9
    agent_overrides: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# TOML serializer (minimal — handles strings, string lists, one-level section)
# ---------------------------------------------------------------------------

def _serialize_toml(config: AppConfig) -> str:
    """Serialize AppConfig to a TOML-formatted string."""
    lines: list[str] = []

    def _quote(s: str) -> str:
        escaped = (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
            .replace("\b", "\\b")
            .replace("\f", "\\f")
        )
        return f'"{escaped}"'

    def _str_list(lst: list[str]) -> str:
        return "[" + ", ".join(_quote(v) for v in lst) + "]"

    lines.append(f"active_agent = {_quote(config.active_agent)}")
    lines.append(f"project_root = {_quote(config.project_root)}")
    lines.append(f"terminal_bottom_height = {config.terminal_bottom_height}")

    if config.agent_overrides:
        lines.append("")
        lines.append("[agent_overrides]")
        for key, cmd in config.agent_overrides.items():
            lines.append(f"{key} = {_str_list(cmd)}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config() -> AppConfig:
    """Load config from disk. Returns defaults if file doesn't exist or is malformed."""
    try:
        path = config_path()
        if not path.exists():
            return AppConfig()
        with path.open("rb") as fh:
            data = tomllib.load(fh)

        active_agent = str(data.get("active_agent", "claude"))
        project_root = str(data.get("project_root", ""))
        raw_bottom_height = data.get("terminal_bottom_height", 9)
        terminal_bottom_height = (
            raw_bottom_height if isinstance(raw_bottom_height, int) else 9
        )

        raw_overrides = data.get("agent_overrides", {})
        agent_overrides: dict[str, list[str]] = {}
        if isinstance(raw_overrides, dict):
            for k, v in raw_overrides.items():
                if isinstance(v, list) and all(isinstance(s, str) for s in v):
                    agent_overrides[k] = v

        return AppConfig(
            active_agent=active_agent,
            project_root=project_root,
            terminal_bottom_height=max(5, min(18, terminal_bottom_height)),
            agent_overrides=agent_overrides,
        )
    except Exception:  # noqa: BLE001
        return AppConfig()


def save_config(config: AppConfig) -> None:
    """Write config to disk, creating parent dirs as needed."""
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialize_toml(config), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"modelmesh: warning: could not save config: {exc}", file=sys.stderr)
