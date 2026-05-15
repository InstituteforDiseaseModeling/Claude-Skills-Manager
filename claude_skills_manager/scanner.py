"""Discover skills from the three canonical sources.

Global  : ~/.claude/skills/<skill>/SKILL.md
Plugin  : ~/.claude/plugins/marketplaces/<m>/.claude-plugin/marketplace.json
          → resolved plugin folders → <plugin>/skills/<skill>/SKILL.md
Project : <project_root>/**/.claude/skills/<skill>/SKILL.md   (recursive)

Plugin discovery is manifest-driven: each marketplace's
``.claude-plugin/marketplace.json`` is the authoritative source for which
plugins exist, the plugin's canonical *name* (used by ``enabledPlugins``,
which can differ from the folder name — e.g. ``idm-docs-plugin`` vs.
``idm_docs_plugin/``), and where each plugin's skills live. Three layouts
seen in the wild are all supported:
  A. ``<m>/plugins/<plugin-name>/skills/...`` (claude-plugins-official)
  B. ``<m>/<source-folder>/skills/...``       (idm-standards, idm-agent-skills)
  C. ``<m>/<source>/`` + explicit ``skills: ["./skills/foo", ...]`` array
     in the manifest                          (anthropic-agent-skills)
A path-walk fallback handles marketplaces with no manifest.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from .models import Skill, SkillType
from .skill_md import parse_skill_md
from .skill_settings import (
    STATE_ON, STATE_PLUGIN_OFF, read_enabled_plugins, read_overrides,
)

# Skip these when walking project trees — both for speed and to avoid
# accidentally picking up vendored skills inside dependency directories.
IGNORED_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".idea", ".vscode", "target", ".next", ".tox",
    "site-packages", ".pytest_cache", ".mypy_cache", ".cache", ".gradle",
})

# Defensive cap so a user pointing at C:\ doesn't hang the UI.
MAX_SCAN_DEPTH = 8


class SkillScanner:
    """Pure-Python skill discovery — no Qt imports, easy to unit test."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = (home or Path.home()).expanduser()

    # ------------------------------------------------------------------ public
    def scan_all(self, project_root: Path | None = None) -> list[Skill]:
        skills: list[Skill] = []
        skills.extend(self.scan_global())
        skills.extend(self.scan_plugin())
        if project_root is not None:
            skills.extend(self.scan_project(project_root))
        skills = _dedupe(skills)
        self._populate_states(skills)
        return skills

    def scan_global(self) -> list[Skill]:
        return self._scan_skill_holder(
            self.home / ".claude" / "skills", SkillType.GLOBAL
        )

    def scan_plugin(self) -> list[Skill]:
        """Discover plugin skills, preferring each marketplace's manifest as
        the source of truth and falling back to the legacy folder walk when
        no manifest is present."""
        marketplaces = self.home / ".claude" / "plugins" / "marketplaces"
        out: list[Skill] = []
        for marketplace in _iter_subdirs(marketplaces):
            manifest = _read_marketplace_manifest(marketplace)
            if manifest is None:
                out.extend(self._scan_marketplace_legacy(marketplace))
            else:
                out.extend(self._scan_marketplace_manifest(marketplace, manifest))
        return out

    def _scan_marketplace_manifest(
        self, marketplace: Path, manifest: dict[str, Any],
    ) -> list[Skill]:
        """Walk a marketplace using its manifest. The manifest's top-level
        ``name`` plus each plugin entry's ``name`` produces the canonical
        ``<plugin>@<marketplace>`` id used by ``enabledPlugins``. We stamp
        that id onto every Skill returned here so ``_populate_states``
        doesn't have to re-derive it from the path (which would mis-match
        when folder names diverge from manifest names — see idm-standards
        where ``idm_docs_plugin/`` corresponds to ``idm-docs-plugin``).
        """
        marketplace_name = manifest.get("name") or marketplace.name
        plugins = manifest.get("plugins")
        if not isinstance(plugins, list):
            return []
        out: list[Skill] = []
        for entry in plugins:
            if not isinstance(entry, dict):
                continue
            plugin_name = entry.get("name")
            if not isinstance(plugin_name, str) or not plugin_name:
                continue
            plugin_id = f"{plugin_name}@{marketplace_name}"
            plugin_dir = _resolve_plugin_dir(marketplace, plugin_name, entry)
            if plugin_dir is None or not plugin_dir.is_dir():
                continue

            # Layout C: explicit skills list — each entry resolves relative
            # to the plugin source. This overrides the default skills/
            # folder walk because the manifest is allowed to expose only a
            # subset of available skill folders.
            explicit = entry.get("skills")
            if isinstance(explicit, list):
                for item in explicit:
                    if not isinstance(item, str):
                        continue
                    skill_dir = (plugin_dir / item).resolve()
                    skill = self._make_skill(skill_dir, SkillType.PLUGIN)
                    if skill is not None:
                        skill.plugin_id = plugin_id
                        out.append(skill)
            else:
                # Layouts A and B: plugin's skills live under <plugin>/skills/.
                discovered = self._scan_skill_holder(plugin_dir / "skills", SkillType.PLUGIN)
                for s in discovered:
                    s.plugin_id = plugin_id
                out.extend(discovered)
        return out

    def _scan_marketplace_legacy(self, marketplace: Path) -> list[Skill]:
        """Fallback for marketplaces without a manifest — walk the legacy
        ``<m>/plugins/<plugin>/skills/`` layout. ``plugin_id`` is derived
        from folder names; this matches manifest-derived ids only when
        plugin folder names equal plugin canonical names (the common case
        for the official marketplace)."""
        marketplace_name = marketplace.name
        out: list[Skill] = []
        for plugin in _iter_subdirs(marketplace / "plugins"):
            discovered = self._scan_skill_holder(plugin / "skills", SkillType.PLUGIN)
            plugin_id = f"{plugin.name}@{marketplace_name}"
            for s in discovered:
                s.plugin_id = plugin_id
            out.extend(discovered)
        return out

    def scan_project(self, project_root: Path) -> list[Skill]:
        project_root = project_root.expanduser().resolve()
        if not project_root.is_dir():
            return []
        out: list[Skill] = []
        for skills_dir in self._find_project_skills_dirs(project_root):
            out.extend(self._scan_skill_holder(skills_dir, SkillType.PROJECT))
        return out

    # ---------------------------------------------------------------- internals
    def _populate_states(self, skills: list[Skill]) -> None:
        """Annotate each skill with its effective visibility state.

        Plugin skills already carry a canonical ``plugin_id`` set by
        ``scan_plugin`` from each marketplace's manifest. We trust that
        rather than re-deriving from the path: plugin folder names can
        differ from canonical plugin names (idm-standards uses
        ``idm_docs_plugin/`` for ``idm-docs-plugin``), and re-deriving
        would mis-match against ``enabledPlugins``.

        Scope derivation for Global/Project skills must match the write
        side (see ``MainWindow._scope_dir_for`` and
        ``skill_settings.write_override``). Each skill's overrides come
        from the ``.claude/`` folder *containing its own skills/
        directory* — i.e., ``skill.path.parents[1]`` — not from a global
        project root. Without this, a project_root pointed at a monorepo
        parent (e.g. ``C:\\projects``) reads ``C:\\projects\\.claude\\``
        while writes go to ``C:\\projects\\<project>\\.claude\\``, so
        toggles silently disappear on Refresh.

        Reads are cached by scope so each settings file is hit once per
        scan even when many skills share a folder."""
        enabled_plugins = read_enabled_plugins(self.home)
        overrides_cache: dict[Path, dict[str, str]] = {}

        def overrides_for(skill: Skill) -> dict[str, str]:
            try:
                scope = skill.path.parents[1]
            except IndexError:
                return {}
            if scope not in overrides_cache:
                overrides_cache[scope] = read_overrides(scope)
            return overrides_cache[scope]

        for s in skills:
            if s.type == SkillType.PLUGIN:
                if s.plugin_id and enabled_plugins.get(s.plugin_id, False):
                    s.state = STATE_ON
                else:
                    s.state = STATE_PLUGIN_OFF
            else:
                # Global and Project both: each skill's overrides live in
                # the .claude/ folder above its skills/ directory — same
                # path the write side targets via skill.path.parents[1].
                s.state = overrides_for(s).get(s.name, STATE_ON)

    def _find_project_skills_dirs(self, root: Path) -> Iterator[Path]:
        """Walk the tree looking for any '.claude/skills' directory."""
        base_depth = str(root).count(os.sep)
        for dirpath, dirnames, _filenames in os.walk(root):
            depth = dirpath.count(os.sep) - base_depth
            if depth > MAX_SCAN_DEPTH:
                dirnames[:] = []
                continue
            # Always allow descending into '.claude' itself; otherwise prune
            # dotfiles and the configured ignore list.
            dirnames[:] = [
                d for d in dirnames
                if d == ".claude"
                or (d not in IGNORED_DIRS and not d.startswith("."))
            ]
            current = Path(dirpath)
            if current.name == "skills" and current.parent.name == ".claude":
                yield current
                # Don't descend further — children of <skill>/ aren't skills.
                dirnames[:] = []

    def _scan_skill_holder(self, skills_dir: Path, kind: SkillType) -> list[Skill]:
        if not skills_dir.is_dir():
            return []
        out: list[Skill] = []
        for child in _iter_subdirs(skills_dir):
            skill = self._make_skill(child, kind)
            if skill is not None:
                out.append(skill)
        return out

    def _make_skill(self, folder: Path, kind: SkillType) -> Skill | None:
        """Build a Skill from a single folder if it contains a SKILL.md.

        Used both by ``_scan_skill_holder`` (default ``skills/`` walks) and
        by Layout C in ``_scan_marketplace_manifest`` where each skill is
        listed by explicit path."""
        skill_md = folder / "SKILL.md"
        if not skill_md.is_file():
            return None
        metadata, description = parse_skill_md(skill_md)
        name_field = metadata.get("name")
        name = name_field if isinstance(name_field, str) and name_field else folder.name
        return Skill(
            name=name,
            path=folder.resolve(),
            type=kind,
            description=description,
            skill_md_path=skill_md,
            metadata=metadata,
        )


# ----------------------------------------------------------------------- helpers
def _read_marketplace_manifest(marketplace: Path) -> dict[str, Any] | None:
    """Return the parsed ``marketplace.json`` for a marketplace, or ``None``
    when it's missing/unreadable/malformed.

    Returning ``None`` (rather than raising) is intentional — discovery
    should degrade gracefully to the legacy path-walk when a marketplace
    happens to lack a manifest, the same permissive-fallback shape as
    ``read_overrides`` in ``skill_settings``."""
    path = marketplace / ".claude-plugin" / "marketplace.json"
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_plugin_dir(
    marketplace: Path, plugin_name: str, entry: dict[str, Any],
) -> Path | None:
    """Locate a plugin's source folder on disk from a manifest entry.

    Three forms are seen in the wild:
      * ``"source": "./folder"`` — string path relative to the marketplace
        root. Used by idm-standards / idm-agent-skills to put plugins as
        direct children of the marketplace.
      * ``"source": {...}`` (dict, e.g. ``"git-subdir"`` install spec) —
        the manifest describes where it came FROM, not where it lives
        now. Claude Code installs these under
        ``<marketplace>/plugins/<plugin-name>/``, so we use that.
      * No ``source`` field — same default as above.

    Returns ``None`` if the resolved path escapes the marketplace tree
    or doesn't resolve cleanly."""
    source = entry.get("source")
    if isinstance(source, str) and source:
        candidate = (marketplace / source).resolve()
    else:
        candidate = (marketplace / "plugins" / plugin_name).resolve()
    return candidate


def _iter_subdirs(p: Path) -> Iterator[Path]:
    if not p.exists() or not p.is_dir():
        return
    try:
        items = sorted(p.iterdir(), key=lambda x: x.name.lower())
    except (PermissionError, OSError):
        return
    for item in items:
        try:
            if item.is_dir():
                yield item
        except OSError:
            continue


def _dedupe(skills: list[Skill]) -> list[Skill]:
    seen: set[Path] = set()
    out: list[Skill] = []
    for s in skills:
        if s.path in seen:
            continue
        seen.add(s.path)
        out.append(s)
    return out
