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
    # Composed effective visibility — what the user will see reflected in
    # Claude Code. Populated by the scanner from the override layer and (for
    # plugin skills) the plugin enablement layer. Defaults to "on" (the
    # absence-default for skillOverrides). For plugin skills whose owning
    # plugin is disabled the scanner sets the synthesized "plugin-off"
    # regardless of the override layer below — see §7.63.
    state: str = "on"
    # Raw skillOverrides value (or default "on" when absent), independent
    # of the plugin enablement layer. For Global/Project this equals
    # ``state``. For Plugin skills it diverges when the parent plugin is
    # disabled: ``state == "plugin-off"`` but ``override_state`` carries
    # whatever the per-skill override is, so the toggle keeps working and
    # the UI can render both layers (§7.63 decision 3).
    override_state: str = "on"
    # For plugin skills only: "<plugin-name>@<marketplace>" — the key under
    # `enabledPlugins` in ~/.claude/settings.json. None for Global/Project.
    plugin_id: str | None = None

    @property
    def display_location(self) -> str:
        try:
            return str(self.path.relative_to(Path.home()))
        except ValueError:
            return str(self.path)
