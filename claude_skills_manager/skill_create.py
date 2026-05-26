"""Create a new Global or Project skill on disk.

Qt-free seam matching :mod:`claude_trust` and :mod:`recycle`: the
GUI's :class:`NewSkillDialog` collects inputs, runs them through the
validators in this module, then calls :func:`create_skill`. No Qt
imports here — the dialog is a thin presentation layer over these
pure functions, and the same logic is reusable from a future CLI or
scripting context.

**Per-type scope.** Global skills land at ``~/.claude/skills/<name>/``;
Project skills land at ``<project_root>/.claude/skills/<name>/``. The
write paths intentionally mirror the *top-level* scan paths used by
:class:`~claude_skills_manager.scanner.SkillScanner` — for Project,
that means the project root's own ``.claude/skills/``, not a nested
workspace's. Choosing among nested ``.claude/`` folders is a
multi-target problem this dialog deliberately doesn't try to solve.

**Plugin skills are not creatable here.** Plugins are authored
upstream and distributed via marketplace manifests; the GUI never
mutates plugin folders (matches the existing Enable/Disable and
Delete asymmetries). Calling :func:`create_skill` with
``SkillType.PLUGIN`` raises :class:`ValueError` defensively — the
dialog's Plugin radio is disabled, but a signal-level guard costs
nothing and protects against a future caller wiring it up wrong."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from .models import SkillType


# Name validation pattern. Lowercase letters + digits + hyphens,
# must start with a letter or digit (leading hyphen would be a
# filesystem oddity on POSIX shells expanding ``-`` as a flag).
# Matches the convention seen in existing skills like
# ``discover-skills``, ``explain-code``, ``sphinx-to-mkdocs``.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Length cap is defensive against Windows long-path limits (260
# chars total path on default-configured Windows). The full path
# is roughly:  <project_root> + ``/.claude/skills/`` + <name> + ``/SKILL.md``.
# 64 chars for the name leaves ample headroom on any reasonable
# project root and matches the longest names actually shipping
# (e.g. ``claude-md-management:revise-claude-md`` is 38 chars).
_NAME_MAX_LEN = 64

# Description cap — descriptions get loaded into Claude's context
# window every time the model decides whether to route to this
# skill, so an accidental README paste would balloon every prompt.
# 1024 chars is generous for a one-sentence-or-paragraph rationale.
_DESCRIPTION_MAX_LEN = 1024

# Windows reserved device names. Forbidden as folder names because
# the OS won't let you create / open / delete them through normal
# filesystem APIs; a folder called ``CON`` would scan correctly but
# fail mysteriously at any open-file operation.
_WINDOWS_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})

# Skeleton SKILL.md written into every newly-created skill folder.
# The frontmatter mirrors the dialog's two inputs (so the skill_md
# parser picks up name + description identically to a hand-authored
# skill). The body sections nudge the user toward the conventional
# structure without forcing it — they can rip them out if the skill
# doesn't fit that shape. Two trailing newlines so the file ends
# cleanly (POSIX convention, and avoids "no newline at end of file"
# diff noise on first save).
_SKILL_MD_TEMPLATE = """\
---
name: {name}
description: {description}
---

# {name}

{description}

## When to use

(Describe situations where Claude should invoke this skill.)

## How it works

(Describe the steps, capabilities, or files this skill provides.)
"""


def validate_skill_name(name: str) -> str | None:
    """Return ``None`` if ``name`` is a valid skill name, otherwise a
    human-readable error message suitable for inline display in the
    dialog.

    The function is intentionally pure: no filesystem access, no
    scope awareness. Pattern + length + reserved-name checks only.
    Taken-ness (whether a folder of that name already exists in the
    chosen scope) is a separate concern handled by
    :func:`is_skill_name_taken` — the dialog runs both, surfacing
    whichever fires first."""
    if not name:
        return "Name is required."
    stripped = name.strip()
    if stripped != name:
        # Leading/trailing whitespace is almost always a paste-error.
        # Reject explicitly rather than silently strip — silent strip
        # would mask the "Name:[<space>foo]" case where the user
        # thinks they typed ``foo`` but the input has a leading space.
        return "Name cannot start or end with whitespace."
    if len(name) > _NAME_MAX_LEN:
        return f"Name is too long (max {_NAME_MAX_LEN} characters)."
    if not _NAME_RE.match(name):
        return (
            "Name must start with a lowercase letter or digit and "
            "contain only lowercase letters, digits, and hyphens.")
    if name.lower() in _WINDOWS_RESERVED_NAMES:
        return f"'{name}' is a reserved name on Windows."
    if name.endswith(("-",)):
        # Trailing hyphen is allowed by the regex but is almost
        # always a typo. Block it; the explicit message points the
        # user at the fix.
        return "Name cannot end with a hyphen."
    return None


def validate_description(description: str) -> str | None:
    """Return ``None`` if ``description`` is acceptable, otherwise a
    human-readable error message.

    Required (Claude relies on descriptions for routing decisions —
    an empty description makes a skill effectively invisible to the
    model). Capped at :data:`_DESCRIPTION_MAX_LEN` so an accidental
    README paste can't bloat every prompt's context."""
    if not description or not description.strip():
        return "Description is required."
    if len(description) > _DESCRIPTION_MAX_LEN:
        return (f"Description is too long "
                f"(max {_DESCRIPTION_MAX_LEN} characters; "
                f"current: {len(description)}).")
    # Reject ASCII control chars except newline (0x0A) and tab (0x09).
    # YAML frontmatter tolerates plain newlines and tabs but breaks
    # on form-feed / vertical-tab / other ctrl bytes — and those
    # have no business in a description anyway.
    for ch in description:
        code = ord(ch)
        if code < 0x20 and code not in (0x09, 0x0A):
            return ("Description contains a control character "
                    f"(code 0x{code:02x}).")
    return None


def scope_dir_for(
    skill_type: SkillType,
    project_root: Path | None,
    *,
    home: Path | None = None,
) -> Path:
    """Return the ``<scope>/.claude/skills/`` directory under which a
    new skill of ``skill_type`` should be created.

    * ``GLOBAL``  → ``~/.claude/skills/`` (override via ``home``).
    * ``PROJECT`` → ``<project_root>/.claude/skills/``. ``project_root``
      must be provided and must exist on disk.
    * ``PLUGIN``  → raises :class:`ValueError`; plugins are not
      creatable from this GUI (see module docstring).

    ``home`` exists for testability — mirrors :class:`SkillScanner`'s
    same-named ctor argument so tests can point at a tempdir without
    monkeypatching ``Path.home``."""
    if skill_type == SkillType.PLUGIN:
        raise ValueError(
            "Plugin skills can't be created from this GUI — they are "
            "authored upstream and distributed via marketplace manifests. "
            "Use /plugin in Claude Code to install plugins.")
    if skill_type == SkillType.GLOBAL:
        base = (home or Path.home()).expanduser()
        return base / ".claude" / "skills"
    if skill_type == SkillType.PROJECT:
        if project_root is None:
            raise ValueError(
                "Project skills require a project root. Choose one in "
                "the main window first.")
        root = project_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(
                f"Project root does not exist: {root}")
        return root / ".claude" / "skills"
    raise ValueError(f"Unknown skill type: {skill_type!r}")


def is_skill_name_taken(name: str, scope_dir: Path) -> bool:
    """Return True if a folder called ``name`` already exists in
    ``scope_dir`` (case-insensitive).

    Case-insensitive because NTFS / APFS treat ``my-skill`` and
    ``My-Skill`` as the same directory; a case-sensitive check
    would let the user pass validation and then collide at
    ``mkdir`` time on Windows. Lower-cased comparison is sound
    here because the validator already constrains ``name`` to
    lowercase ASCII — if it matches a differently-cased entry on
    disk, that entry was almost certainly created by this same
    flow on a different machine."""
    if not scope_dir.exists():
        return False
    needle = name.lower()
    try:
        for entry in scope_dir.iterdir():
            if entry.is_dir() and entry.name.lower() == needle:
                return True
    except OSError:
        # Can't list the directory (permission denied, broken
        # symlink, etc.). Treat as "not taken" and let mkdir
        # surface the real error — better than masking a
        # filesystem problem behind a validation message.
        return False
    return False


def create_skill(
    *,
    name: str,
    description: str,
    skill_type: SkillType,
    project_root: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Create the skill folder and its initial ``SKILL.md`` on disk.
    Return the absolute path to the created ``SKILL.md``.

    Argument order is keyword-only because the call site reads
    much more clearly as
    ``create_skill(name=..., description=..., skill_type=...)``
    than as four positional args of similar types.

    Raises
    ------
    ValueError
        ``name`` or ``description`` failed validation, or
        ``skill_type`` is :attr:`SkillType.PLUGIN`, or
        ``skill_type`` is :attr:`SkillType.PROJECT` without a
        valid ``project_root``.
    FileExistsError
        A skill folder of that name already exists in the chosen
        scope.
    OSError
        Filesystem write failed (mkdir, temp-file write, atomic
        rename). On partial failure the function attempts to
        remove the half-created folder so the user doesn't end up
        with a phantom skill in the Refresh list.

    Write order is atomic in the same shape as :mod:`claude_trust`:
    write SKILL.md to a temp file in the same directory, then
    ``os.replace`` into place. A crash between mkdir and rename
    leaves an empty folder (which we clean up); a crash during
    write leaves only the temp file (renamed-out, not visible to
    the scanner)."""
    name_err = validate_skill_name(name)
    if name_err:
        raise ValueError(name_err)
    desc_err = validate_description(description)
    if desc_err:
        raise ValueError(desc_err)

    skills_dir = scope_dir_for(skill_type, project_root, home=home)
    if is_skill_name_taken(name, skills_dir):
        raise FileExistsError(
            f"A skill called '{name}' already exists in {skills_dir}.")

    skill_dir = skills_dir / name
    skill_md = skill_dir / "SKILL.md"

    # ``parents=True`` covers the case where ``.claude/skills/``
    # doesn't exist yet — Global users with no prior skills, or a
    # fresh project root. ``exist_ok=True`` on the parent is fine
    # because the is_skill_name_taken check above already gated the
    # leaf; this only relaxes the intermediate dirs.
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        # ``exist_ok=False`` on the leaf so a race (the directory
        # being created externally between our check and now) is
        # surfaced as FileExistsError instead of being silently
        # collided into.
        skill_dir.mkdir(exist_ok=False)
    except FileExistsError:
        # Re-raise with a more useful message — Python's default
        # FileExistsError("[Errno 17] File exists: '...'") tells
        # the user nothing about which collision occurred.
        raise FileExistsError(
            f"A folder called '{name}' already exists in {skills_dir}.")
    except OSError:
        # mkdir on intermediate dirs failed (permission denied, disk
        # full, read-only mount). Let it propagate — there's no
        # half-created state to clean up because the leaf mkdir
        # hasn't run yet.
        raise

    # Atomic write: build SKILL.md in a temp file in the same
    # directory, then rename into place. Same directory matters —
    # os.replace across filesystems is not atomic. Cleanup-on-error
    # tears down both the temp file (if it survived) and the empty
    # leaf folder we just created, so a failed write doesn't leave
    # behind a phantom skill that would confuse the scanner.
    content = _SKILL_MD_TEMPLATE.format(
        name=name,
        description=description.strip(),
    )
    fd = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".SKILL.md.", dir=str(skill_dir))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        fd = None  # ownership transferred to the with-block; don't re-close
        os.replace(tmp_path, str(skill_md))
        tmp_path = None  # rename succeeded; no cleanup needed
    except OSError:
        # Clean up in reverse order: temp file (if it still exists),
        # then the empty leaf folder. shutil.rmtree on a folder that
        # was *just* created by us is safe — we can't be deleting
        # the user's data because we just made the directory.
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        try:
            shutil.rmtree(skill_dir)
        except OSError:
            pass
        raise

    return skill_md
