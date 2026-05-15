"""Core data structures used across the app — deliberately Qt-free so the
domain logic (scanning, parsing) stays unit-testable from a plain script."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SkillType(str, Enum):
    GLOBAL = "Global"
    PROJECT = "Project"
    PLUGIN = "Plugin"


@dataclass
class Skill:
    name: str
    path: Path                       # absolute folder path; deduplication key
    type: SkillType
    description: str = ""
    skill_md_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Visibility per Claude Code's skillOverrides; populated by the scanner
    # after discovery. Defaults to "on" (Claude Code's effective default for
    # skills absent from skillOverrides). For plugin skills whose owning
    # plugin is disabled the scanner sets the synthesized "plugin-off".
    state: str = "on"
    # For plugin skills only: "<plugin-name>@<marketplace>" — the key under
    # `enabledPlugins` in ~/.claude/settings.json. None for Global/Project.
    plugin_id: str | None = None

    @property
    def display_location(self) -> str:
        try:
            return str(self.path.relative_to(Path.home()))
        except ValueError:
            return str(self.path)
