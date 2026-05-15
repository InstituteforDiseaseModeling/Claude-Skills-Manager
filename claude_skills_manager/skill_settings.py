"""Read and write Claude Code's skill-visibility settings.

Authoritative reference: https://code.claude.com/docs/en/skills.md#override-skill-visibility-from-settings

Claude Code stores per-skill visibility in a `skillOverrides` map inside
``settings.json`` and/or ``settings.local.json`` under the relevant ``.claude``
folder (``~/.claude`` for Global skills, ``<project>/.claude`` for Project
skills). Plugin skills are NOT controlled this way — they inherit their
plugin's enabled/disabled state from ``enabledPlugins`` in
``~/.claude/settings.json``.

This module is deliberately Qt-free so it can be unit-tested from a plain
Python script and reused by a CLI / web frontend later, matching the same
layering rule as ``scanner.py`` and ``skill_md.py``."""
from __future__ import annotations

import json
from pathlib import Path

# Authoritative state values per the Claude Code docs. A skill that is absent
# from `skillOverrides` is treated as STATE_ON (default).
STATE_ON                  = "on"
STATE_OFF                 = "off"
STATE_NAME_ONLY           = "name-only"
STATE_USER_INVOCABLE_ONLY = "user-invocable-only"

# Synthesized state for plugin skills whose owning plugin is disabled. Not a
# real value in skillOverrides — it's only used inside this app to render
# the inherited disabled state in the UI.
STATE_PLUGIN_OFF = "plugin-off"

ALL_OVERRIDE_STATES: frozenset[str] = frozenset({
    STATE_ON, STATE_OFF, STATE_NAME_ONLY, STATE_USER_INVOCABLE_ONLY,
})
BINARY_STATES: frozenset[str] = frozenset({STATE_ON, STATE_OFF})


# ---------------------------------------------------------------------- reads
def read_overrides(claude_dir: Path) -> dict[str, str]:
    """Merged ``skillOverrides`` from ``settings.json`` and
    ``settings.local.json`` inside ``claude_dir`` (``.local`` wins on key
    collision, matching how Claude Code itself resolves the two).

    Returns an empty dict if neither file exists, either is unreadable, or
    the ``skillOverrides`` block is missing/malformed. Silent failure on
    read is intentional — display should degrade to "everything looks
    enabled" rather than crashing on a stray comma."""
    merged: dict[str, str] = {}
    for filename in ("settings.json", "settings.local.json"):
        path = claude_dir / filename
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        overrides = data.get("skillOverrides")
        if not isinstance(overrides, dict):
            continue
        for key, value in overrides.items():
            if isinstance(value, str) and value in ALL_OVERRIDE_STATES:
                merged[key] = value
    return merged


def read_enabled_plugins(home: Path) -> dict[str, bool]:
    """``enabledPlugins`` from ``~/.claude/settings.json``. Empty dict on any
    failure. Keys are ``<plugin>@<marketplace>``; absence means disabled."""
    path = home / ".claude" / "settings.json"
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    plugins = data.get("enabledPlugins")
    if not isinstance(plugins, dict):
        return {}
    return {k: bool(v) for k, v in plugins.items() if isinstance(k, str)}


# --------------------------------------------------------------------- writes
def write_override(claude_dir: Path, skill_name: str, state: str | None) -> None:
    """Set or clear an override in ``<claude_dir>/settings.local.json``.

    ``state=None`` or ``state=='on'`` → remove the entry (absent == default).
    Other valid states → write the literal string.

    The write is read-modify-write so all other keys are preserved.

    Raises:
        ValueError: existing JSON is malformed or not an object.
        ValueError: state is non-empty and not one of ``ALL_OVERRIDE_STATES``.
        OSError: file cannot be written.
    """
    if state is not None and state not in ALL_OVERRIDE_STATES:
        raise ValueError(f"Invalid skill state: {state!r}")

    settings_path = claude_dir / "settings.local.json"
    if settings_path.is_file():
        try:
            with settings_path.open(encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            # Don't auto-fix: overwriting would silently drop other settings
            # (permissions, env vars, hooks). The user must repair the JSON.
            raise ValueError(f"{settings_path} contains invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"{settings_path} is not a JSON object")
    else:
        data = {}

    overrides = data.get("skillOverrides")
    if not isinstance(overrides, dict):
        overrides = {}

    if state is None or state == STATE_ON:
        overrides.pop(skill_name, None)
    else:
        overrides[skill_name] = state

    if overrides:
        data["skillOverrides"] = overrides
    else:
        # Don't leave an empty `skillOverrides: {}` behind — keeps the file
        # minimal and matches what /skills produces.
        data.pop("skillOverrides", None)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
