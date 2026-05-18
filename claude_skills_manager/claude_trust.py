"""Read / write the Claude CLI's per-directory trust state.

The Claude CLI persists which directories the user has agreed to
"trust" (i.e. allow filesystem reads from) in ``~/.claude.json``,
under ``projects.<absolute-forward-slash-path>.hasTrustDialogAccepted``.
When that flag is missing or False for the CLI's working directory,
``claude -p`` opens an interactive trust prompt — which a
non-interactive ``QProcess`` / ``Popen`` invocation has no way to
answer, so the run hangs until timeout.

This module is the seam the GUI uses to mirror Claude Desktop's
"Trust this folder" affordance: check whether a directory is
trusted, and (after user confirmation) mark it trusted.

Qt-free by design — the GUI does the prompting; this module only
touches the JSON file. Writes are atomic via temp-file + ``os.replace``
so a crash mid-write can't half-corrupt the user's config.

Key shape gotcha: the CLI keys ``projects`` by **forward-slash**
absolute paths, even on Windows (e.g. ``C:/work/...``). Writing
with backslashes creates a duplicate entry that the CLI ignores,
so the trust prompt keeps firing. :func:`normalize_trust_key`
centralizes that conversion."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional


def claude_config_path() -> Path:
    """Return the on-disk location of the Claude CLI config file
    (``~/.claude.json`` on every supported platform — ``Path.home()``
    resolves correctly on Windows via ``%USERPROFILE%`` and on POSIX
    via ``$HOME`` so we don't have to branch)."""
    return Path.home() / ".claude.json"


def normalize_trust_key(path: Path) -> str:
    """Convert a filesystem path into the exact key shape the Claude
    CLI uses inside ``projects`` in ``~/.claude.json``: an absolute
    path with forward slashes.

    Verified empirically against existing trusted entries — Windows
    keys are ``C:/work/...``, not ``C:\\work\\...``. Failing to
    match this shape writes a duplicate entry that the CLI ignores,
    so the trust prompt keeps firing despite our "fix"."""
    return str(path.resolve()).replace("\\", "/")


def is_path_trusted(path: Path) -> Optional[bool]:
    """Return the trust state for ``path`` recorded by the Claude CLI:

    * ``True``  — entry exists and ``hasTrustDialogAccepted`` is True
    * ``False`` — entry missing or flag is not True
    * ``None``  — ``~/.claude.json`` doesn't exist or can't be parsed
      (the CLI has never run; we can't determine state and shouldn't
      pretend to)

    ``None`` is distinct from ``False`` because the caller's response
    differs: for ``False`` the caller should prompt to trust; for
    ``None`` the caller should leave the file alone (writing a stub
    with only one key would erase the rest of the CLI's state on
    first read)."""
    cfg = claude_config_path()
    if not cfg.exists():
        return None
    try:
        with cfg.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    entry = projects.get(normalize_trust_key(path))
    if not isinstance(entry, dict):
        return False
    return entry.get("hasTrustDialogAccepted") is True


def mark_path_trusted(path: Path) -> None:
    """Set ``hasTrustDialogAccepted = True`` for ``path`` in
    ``~/.claude.json``, creating the ``projects.<key>`` entry if it
    doesn't exist. Idempotent — calling on an already-trusted path
    just rewrites the file with the same contents.

    Raises ``FileNotFoundError`` if ``~/.claude.json`` doesn't yet
    exist (CLI hasn't been initialized). Caller decides what to do
    in that case; we deliberately don't create a stub file because
    that would erase per-key state the CLI hasn't written yet but
    will need on first run.

    **Atomicity:** read → mutate the single key → write a sibling
    temp file → ``os.replace`` to swap. The same-directory temp is
    deliberate: ``os.replace`` across filesystems isn't atomic on
    Windows. A crash before the replace leaves the original intact;
    after the replace, the new file is intact. No half-written state.

    **Race window:** the live Claude CLI also writes this file
    (e.g. updating ``lastSessionId`` after a run). If the CLI writes
    between our read and our replace, we overwrite that write. Same
    risk Claude Desktop runs with — accepted by both tools as a
    low-frequency hazard."""
    cfg = claude_config_path()
    if not cfg.exists():
        raise FileNotFoundError(
            f"Claude config not found at {cfg}. Run `claude` once "
            "interactively to initialize it before trusting folders "
            "from this GUI.")
    with cfg.open("r", encoding="utf-8") as f:
        data = json.load(f)
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects
    key = normalize_trust_key(path)
    entry = projects.get(key)
    if not isinstance(entry, dict):
        entry = {}
        projects[key] = entry
    entry["hasTrustDialogAccepted"] = True

    fd, tmp_path = tempfile.mkstemp(
        prefix=".claude.json.", suffix=".tmp", dir=str(cfg.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(cfg))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
