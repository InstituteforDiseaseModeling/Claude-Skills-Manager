"""Relocate a Global or Project skill across scopes.

Qt-free seam matching :mod:`claude_trust`, :mod:`recycle`, and
:mod:`skill_create`: the GUI's drag-drop handler resolves the
gesture into a ``Skill`` + target ``SkillType``, then calls one of
:func:`copy_skill` or :func:`move_skill` here.

**Per-type rule.** Plugin skills are upstream-only — both as a source
and as a target — per the CLAUDE.md per-type mutation rule. Both
functions raise :class:`ValueError` for any PLUGIN-typed argument,
matching the defensive pattern in :func:`skill_create.scope_dir_for`
(line 175-179). The GUI gates the drag and drop at the UI layer, so
in practice these guards should never fire — but defense in depth
protects against a future caller wiring it up wrong.

**No session-id migration.** Earlier iterations (§7.65 / §7.67)
persisted Test Skill conversation ids across dialog closes, which
made :func:`move_skill` responsible for carrying that state across
the path change. §7.68 dropped cross-open persistence entirely —
the Test Skill dialog now starts a brand new session on every open
— so this module no longer touches conversation state at all. The
in-memory session of any currently-open Test Skill window is
unaffected by a move; closing that window after a move (or before)
is what drops it."""
from __future__ import annotations

import shutil
from pathlib import Path

from .models import Skill, SkillType
from .recycle import send_to_recycle_bin
from .skill_create import scope_dir_for


class RelocationCollision(Exception):
    """A folder of the same name already exists at the destination scope.

    Raised by :func:`copy_skill` and :func:`move_skill` *before* any
    on-disk mutation, so callers can surface a clean "rename or
    delete the existing one first" error without partial-state
    rollback. The ``name`` and ``destination`` attributes are
    available for callers that want to render a richer message than
    the default ``str(exc)``."""

    def __init__(self, name: str, destination: Path) -> None:
        super().__init__(
            f"A skill called '{name}' already exists in {destination}.")
        self.name = name
        self.destination = destination


class PartialMoveError(Exception):
    """Move's copy step succeeded but the source removal failed.

    The new copy is fully populated at ``new_path`` and the persisted
    session id (if any) has already been migrated there. The source
    folder still exists at ``source_path`` and must be removed
    manually — typically by the user via Explorer / the Recycle Bin.

    The GUI surfaces this as a "Move was demoted to a Copy" message:
    not an outright failure (the new copy is correct), but the user's
    intent wasn't fully satisfied because the source recycle failed
    (file lock, permission, antivirus scan, etc.)."""

    def __init__(
        self,
        source_path: Path,
        new_path: Path,
        cause: str,
    ) -> None:
        super().__init__(
            f"Move copied to {new_path} but couldn't remove "
            f"{source_path}: {cause}")
        self.source_path = source_path
        self.new_path = new_path
        self.cause = cause


def resolve_destination(
    source: Skill,
    target_type: SkillType,
    *,
    project_root: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Return the absolute path where ``source`` would land if
    relocated to a scope of ``target_type``. Pure helper — useful
    for the confirmation dialog's "To:" field and for callers that
    want to pre-check for collisions before showing the dialog.

    Validates both the source and target type the same way the
    mutating helpers do, so a forbidden combination (e.g. Plugin
    source) raises at resolve-time rather than confusing the caller
    with a partial workflow."""
    if source.type == SkillType.PLUGIN:
        raise ValueError(
            "Plugin skills can't be relocated — they are upstream "
            "artifacts managed via /plugin in Claude Code.")
    if target_type == SkillType.PLUGIN:
        raise ValueError(
            "Cannot relocate to Plugin scope — plugins are upstream "
            "artifacts. Use /plugin in Claude Code instead.")
    scope_dir = scope_dir_for(target_type, project_root, home=home)
    return scope_dir / source.path.name


def _check_collision(name: str, destination_dir: Path) -> None:
    """Raise :class:`RelocationCollision` if ``destination_dir`` already
    contains a folder named ``name`` (case-insensitive).

    Case-insensitive because NTFS / APFS treat ``my-skill`` and
    ``My-Skill`` as the same directory — same reasoning as
    :func:`skill_create.is_skill_name_taken`. If we can't enumerate
    the directory (permission denied, broken symlink), fall through
    and let the actual copy raise the real error; masking a
    filesystem problem behind a collision message would mislead the
    user."""
    if not destination_dir.exists():
        return
    needle = name.lower()
    try:
        for entry in destination_dir.iterdir():
            if entry.is_dir() and entry.name.lower() == needle:
                raise RelocationCollision(name, destination_dir)
    except OSError:
        return


def copy_skill(
    source: Skill,
    target_type: SkillType,
    *,
    project_root: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Copy ``source``'s folder to the ``target_type`` scope. Return
    the absolute path to the new copy.

    The source is untouched; the new copy is a full recursive
    duplicate including SKILL.md, any helper scripts, nested
    directories, and symlinks (preserved as symlinks via
    ``shutil.copytree(..., symlinks=True)`` rather than dereferenced).

    The §7.65 persisted session id is NOT copied — the new skill
    starts a fresh Test Skill conversation. Copying the session id
    would mean two skills resuming the same Claude thread, which is
    incoherent: `claude --resume <id>` consumes the thread, so the
    second skill to Run would either error out or hijack the
    conversation from the first.

    Raises
    ------
    ValueError
        ``source`` is a PLUGIN skill, or ``target_type`` is PLUGIN,
        or ``target_type`` is PROJECT without a valid ``project_root``.
    RelocationCollision
        The destination scope already contains a folder of the same
        name. Raised *before* any on-disk mutation.
    OSError
        The copy itself failed (permission denied, disk full, race
        with external creation of the destination, etc.)."""
    if source.type == SkillType.PLUGIN:
        raise ValueError(
            "Plugin skills can't be relocated — they are upstream "
            "artifacts managed via /plugin in Claude Code.")
    if target_type == SkillType.PLUGIN:
        raise ValueError(
            "Cannot relocate to Plugin scope — plugins are upstream "
            "artifacts. Use /plugin in Claude Code instead.")

    scope_dir = scope_dir_for(target_type, project_root, home=home)
    _check_collision(source.path.name, scope_dir)

    # parents=True covers the case where ~/.claude/skills/ doesn't
    # exist yet (a fresh Global scope on a machine with no prior
    # Global skills). exist_ok=True on the parent is fine — the
    # leaf is the deduplication key and we just verified it doesn't
    # collide.
    scope_dir.mkdir(parents=True, exist_ok=True)
    destination = scope_dir / source.path.name

    # dirs_exist_ok=False so a race (destination created externally
    # between _check_collision and now) surfaces as a clean OSError
    # rather than silently merging two skills into one folder.
    # symlinks=True preserves any symlink inside the skill folder
    # as-symlink — important for skills that link to shared
    # resources rather than embed them. ignore_dangling_symlinks
    # defaults to False, so a broken symlink raises OSError, which
    # is the right surface (the new copy would be subtly broken).
    shutil.copytree(
        str(source.path),
        str(destination),
        symlinks=True,
        dirs_exist_ok=False,
    )
    return destination


def move_skill(
    source: Skill,
    target_type: SkillType,
    *,
    project_root: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Move ``source``'s folder to the ``target_type`` scope. Returns
    the absolute path to the new location.

    Implemented as :func:`copy_skill` followed by
    :func:`recycle.send_to_recycle_bin` on the source — the OS
    Recycle Bin is the user's undo path if Move was a mistake.
    ``shutil.move`` would be tempting but conflates "rename in
    place" (fast, atomic on same-volume) with "copy + delete"
    (slow, non-atomic across volumes) in ways that produce
    different failure modes; explicit copy + recycle gives us
    one well-understood failure shape regardless of source/target
    volume.

    Migrates the persisted §7.65 session id from the source path
    to the new path *before* recycling the source. See module
    docstring for the rationale.

    Raises
    ------
    ValueError, RelocationCollision, OSError
        Same as :func:`copy_skill` (the copy is the first step;
        failures there leave no on-disk mutation).
    PartialMoveError
        Copy succeeded but the recycle step failed. The new
        location is fully populated and the persisted session id
        has been migrated there; the source folder still exists
        on disk and the caller should surface a "Move demoted to
        Copy" message. Recoverable by manual deletion of the
        source via Explorer."""
    new_path = copy_skill(
        source,
        target_type,
        project_root=project_root,
        home=home,
    )

    try:
        send_to_recycle_bin(source.path)
    except (OSError, NotImplementedError) as exc:
        raise PartialMoveError(
            source.path, new_path, str(exc)) from exc

    return new_path
