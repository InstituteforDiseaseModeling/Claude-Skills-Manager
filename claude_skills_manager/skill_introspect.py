"""Qt-free helpers for the Skill Test dialog (§7.34).

Two responsibilities, deliberately separated:

1. **Information extraction** from a ``Skill``'s ``SKILL.md`` — summary,
   examples block, raw text — used by the dialog's read-only tabs.
2. **CLI command construction** for invoking ``claude`` against a
   prompt. The dialog owns the Qt-side ``QProcess`` lifecycle, but
   the *shape* of the invocation (executable, flags, working dir) is
   computed here so this file can stay Qt-free per CLAUDE.md's
   layering rule.

The module imports nothing from PySide6 — verified by inspection. If a
future change pulls a Qt symbol in, the layering rule is broken and
the unit-testable seam disappears with it."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

from .models import Skill, SkillType


# ------------------------------------------------------------- info extract

def read_skill_md_text(skill: Skill) -> str:
    """Return the on-disk contents of ``skill.skill_md_path``, or ``""``
    on any failure (missing file, OS error). Callers render the empty
    string as "no SKILL.md available", which is the right degradation
    for a skill folder that lost its manifest."""
    path = skill.skill_md_path
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def extract_summary(metadata: dict, body: str, *, limit: int = 240) -> str:
    """One-line summary suitable for the test dialog's header strip.

    Order of preference, mirroring how Claude Code itself ranks skill
    info:

    1. Frontmatter ``description:`` field — canonical, because that's
       the text Claude uses for skill-matching against the user's
       prompt. If the author cared enough to write it, surfacing it
       verbatim is the right call.
    2. First non-heading paragraph of the body — fallback used by
       :mod:`skill_md` for the same reason.
    3. Empty string — caller is expected to handle the no-summary case
       gracefully (e.g., hide the row entirely)."""
    desc = metadata.get("description")
    if isinstance(desc, str) and desc.strip():
        text = desc.strip()
    else:
        text = _first_paragraph(body)
    if not text:
        return ""
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def extract_examples_markdown(metadata: dict, body: str) -> str:
    """Return a markdown block describing the skill's example usages.

    Sources, in priority order:

    1. Frontmatter ``examples:`` field (string or list of strings).
       Convention in the wild varies — some authors stash a single
       canonical example here, others a bulleted list. Both shapes are
       rendered as a bulleted block under a synthesized heading.
    2. The first ``## Examples`` / ``## Usage`` / ``## When to use``
       section in the body. Returned verbatim so any code fences,
       sub-headings, or formatting the author wrote survives.
    3. A placeholder explaining the absence and reminding the user
       that Claude Code auto-invokes skills via description-matching.

    Return value is markdown ready for ``QTextBrowser.setMarkdown`` —
    no HTML escaping is required by the caller."""
    fm_examples = metadata.get("examples")
    if isinstance(fm_examples, list) and fm_examples:
        items = "\n".join(
            f"- {str(e).strip()}" for e in fm_examples if str(e).strip())
        if items:
            return f"## Examples (from frontmatter)\n\n{items}"
    if isinstance(fm_examples, str) and fm_examples.strip():
        return f"## Examples (from frontmatter)\n\n{fm_examples.strip()}"

    section = _extract_section(
        body, ("examples", "example", "usage", "how to use", "when to use"))
    if section:
        return section

    return (
        "*No examples were found in this SKILL.md.*\n\n"
        "Most skills are **auto-invoked** by Claude when the user's prompt "
        "matches the skill's description. Try a prompt that matches the "
        "skill's described domain — or use the **Test** tab to issue one "
        "directly to `claude` and see how it responds.")


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _extract_section(body: str, keywords: tuple[str, ...]) -> str:
    """Find the first heading whose text starts with any keyword in
    ``keywords`` (case-insensitive, prefix match) and return that
    heading plus everything up to the next heading at the same or
    shallower depth.

    Empty string if no matching heading exists. Prefix matching (not
    exact equality) catches common variations like "Examples", "Example
    Prompts", "Usage", "Usage notes"."""
    lines = body.splitlines()
    start = -1
    level = 0
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        if start == -1:
            text_lower = m.group(2).lower().strip()
            if any(text_lower.startswith(kw) for kw in keywords):
                start = i
                level = len(m.group(1))
        else:
            this_level = len(m.group(1))
            if this_level <= level:
                return "\n".join(lines[start:i]).strip()
    if start == -1:
        return ""
    return "\n".join(lines[start:]).strip()


def _first_paragraph(text: str, limit: int = 500) -> str:
    """Walk ``text`` until we find the first non-empty, non-heading
    paragraph; return it joined into a single line (newlines collapsed
    to spaces), truncated to ``limit`` chars.

    Duplicated from ``skill_md._first_paragraph`` rather than imported
    because the helper there is module-private (leading underscore).
    The 7-line duplication is cheaper than the alternatives — promoting
    it to public API ripples through the existing module, and importing
    a private symbol forces an "I really mean it" convention violation
    that future readers would (rightly) flag."""
    collected: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped:
            if collected:
                break
            continue
        collected.append(stripped)
    return " ".join(collected)[:limit]


# ------------------------------------------------------------ CLI invocation

# Default Claude Code executable name. Resolution is left to the OS
# PATH — the typical user of this app already has ``claude`` on PATH
# (they've used Claude Code at least once to authenticate). On Windows
# the shim is ``claude.cmd`` (npm-style batch wrapper); calling plain
# ``claude`` from QProcess fails with "file not found" because Windows
# only auto-resolves the ``.cmd`` extension when launched through cmd.exe,
# not when QProcess.start() looks the program up via CreateProcessW.
CLAUDE_EXECUTABLE = "claude.cmd" if sys.platform == "win32" else "claude"

# Common on-disk install locations for the `claude` CLI on Windows.
# Checked in order if ``shutil.which`` can't locate it on PATH — covers
# the very common case where the user installed via ``npm install -g``
# but the npm-global directory (``%APPDATA%\npm``) isn't on the PATH
# that our Python process inherited. Path templates use the literal
# Windows env-var notation; ``os.path.expandvars`` substitutes at
# resolution time.
#
# The order roughly tracks install-method prevalence:
# 1. npm-global (most common for Claude Code)
# 2. ~/.local/bin (pipx / user-local installs — observed in the wild)
# 3. Per-user installer locations
# 4. System-wide installer locations
_WINDOWS_CLAUDE_CANDIDATES: tuple[str, ...] = (
    # npm-global install
    r"%APPDATA%\npm\claude.cmd",
    r"%APPDATA%\npm\claude.bat",
    r"%APPDATA%\npm\claude.ps1",
    r"%APPDATA%\npm\claude",
    # User-local installs (pipx, manual, "%USERPROFILE%\.local\bin"
    # is the Linux-style convention some installers replicate on
    # Windows; one real user's install lived here as claude.EXE)
    r"%USERPROFILE%\.local\bin\claude.exe",
    r"%USERPROFILE%\.local\bin\claude.EXE",
    r"%USERPROFILE%\.local\bin\claude.cmd",
    r"%USERPROFILE%\.local\bin\claude",
    # Per-user installer
    r"%LOCALAPPDATA%\AnthropicClaude\claude.exe",
    r"%LOCALAPPDATA%\Programs\claude\claude.exe",
    r"%LOCALAPPDATA%\Programs\Claude\claude.exe",
    r"%LOCALAPPDATA%\Claude\claude.exe",
    # System-wide installer
    r"%ProgramFiles%\Claude\claude.exe",
    r"%ProgramFiles%\AnthropicClaude\claude.exe",
)


def find_claude_executable() -> str | None:
    """Locate the ``claude`` CLI on disk and return a fully-qualified
    path suitable for passing as ``argv[0]`` to ``QProcess.start``.

    Resolution order:

    1. ``shutil.which("claude")`` — searches PATH respecting
       ``PATHEXT`` on Windows, so it finds ``claude.cmd`` /
       ``claude.exe`` / ``claude.bat`` / ``claude.ps1`` transparently.
       This is the canonical lookup and covers the happy path.
    2. On Windows only, probe the locations in
       ``_WINDOWS_CLAUDE_CANDIDATES``. Catches the case where the
       user *has* installed ``claude`` (e.g. ``npm install -g
       @anthropic-ai/claude-code``) but the install directory isn't
       on the PATH inherited by our Python process. This happens
       routinely when Python was launched from a context that
       snapshotted PATH before the user's environment was fully
       loaded (old terminal session, scheduled task, etc.).

    Returns ``None`` if no candidate exists. Callers should fall
    back to :data:`CLAUDE_EXECUTABLE` (bare name) so ``QProcess``
    can still try its own OS-level resolution, but a ``FailedToStart``
    at that point is genuine and worth surfacing as a diagnostic.

    The function does fresh disk + PATH probes on every call — no
    caching — because the overhead is microseconds and an
    install-during-app-runtime should be picked up the next time
    the user clicks Run."""
    # 1. Canonical PATH lookup
    found = shutil.which("claude")
    if found:
        return found
    # 2. Windows fallback locations
    if sys.platform == "win32":
        for template in _WINDOWS_CLAUDE_CANDIDATES:
            candidate = os.path.expandvars(template)
            # Skip if an env var failed to expand — `%FOO%` left
            # in the string means there's no FOO in the environment,
            # so the path is meaningless.
            if "%" in candidate:
                continue
            if Path(candidate).is_file():
                return candidate
    return None


def claude_path_diagnostic() -> str:
    """Build a human-readable diagnostic describing where we looked
    for ``claude`` and what we found. Surfaced in the health-check
    dialog's output pane when ``QProcess`` fails to start ``claude``
    — the user needs to know exactly which paths we probed and what
    PATH we're inheriting, so they can either install ``claude`` or
    fix their PATH.

    Pure-string output (no Qt) so it can be unit-tested or copied
    into a bug report verbatim."""
    lines: list[str] = []

    found = shutil.which("claude")
    if found:
        lines.append(f"shutil.which('claude')  →  {found}")
    else:
        lines.append("shutil.which('claude')  →  NOT FOUND on PATH")

    if sys.platform == "win32":
        lines.append("")
        lines.append("Probed common Windows install locations:")
        any_found = False
        for template in _WINDOWS_CLAUDE_CANDIDATES:
            candidate = os.path.expandvars(template)
            if "%" in candidate:
                # Env var didn't expand — show that explicitly so the
                # user understands why this candidate was skipped.
                lines.append(f"  [skipped — env var missing]  {template}")
                continue
            if Path(candidate).is_file():
                any_found = True
                lines.append(f"  [FOUND]    {candidate}")
            else:
                lines.append(f"  [missing]  {candidate}")
        if any_found:
            lines.append("")
            lines.append("  → A `claude` binary was found at the FOUND "
                         "location(s) above, but `shutil.which` couldn't")
            lines.append("    locate it via PATH. The app will now try "
                         "that fully-qualified path directly.")

    lines.append("")
    path_str = os.environ.get("PATH", "")
    path_dirs = [d for d in path_str.split(os.pathsep) if d]
    lines.append(f"Current PATH (inherited by this Python process — "
                 f"{len(path_dirs)} entries):")
    for d in path_dirs:
        lines.append(f"  {d}")

    lines.append("")
    lines.append("How to fix:")
    lines.append("  * If `claude` is not installed:  "
                 "`npm install -g @anthropic-ai/claude-code`")
    lines.append("    (or follow the official Claude Code install docs)")
    lines.append("  * If `claude` is installed but not on PATH:  add its "
                 "directory to your")
    lines.append("    user PATH environment variable, then close and "
                 "reopen this app.")
    lines.append("    (PATH changes don't apply to already-running "
                 "processes.)")
    lines.append("  * To verify outside this app, open a fresh "
                 "PowerShell and run:  where claude")

    return "\n".join(lines)


def build_claude_command(
    prompt: str,
    *,
    model: str = "",
    session_id: str = "",
    json_output: bool = False,
    skip_permissions: bool = False,
    extra_read_dirs: Iterable[str | Path] | None = None,
) -> list[str]:
    """Argv list for invoking ``claude`` in one-shot, non-interactive
    mode against ``prompt``.

    ``claude --print <prompt>`` runs Claude Code's print mode: read the
    prompt from argv, emit the assistant's response to stdout, exit.
    The user's existing skill state (``enabledPlugins`` +
    ``skillOverrides`` under ``~/.claude``) is honored automatically —
    that's the whole reason we shell out to ``claude`` instead of
    calling the Anthropic API directly. The test runs against the
    *same* skill configuration the user is managing in this app.

    ``argv[0]`` is the fully-qualified path returned by
    :func:`find_claude_executable` when available, falling back to
    the bare :data:`CLAUDE_EXECUTABLE` name so ``QProcess`` can still
    attempt its own OS-level resolution. Handing QProcess a full
    path removes Windows ``CreateProcessW`` PATH-search behavior
    from the equation — a frequent source of "works in PowerShell,
    doesn't work in QProcess" bug reports.

    ``model`` (optional) inserts ``--model <name>`` before the prompt
    when non-empty. Pinning a model is a *user* decision (Settings →
    Model); the empty default means "let ``claude`` pick its own
    default" so the unset case round-trips exactly like the previous
    no-flag invocation.

    ``session_id`` (optional) inserts ``--resume <id>`` so this turn
    continues an existing Claude Code conversation rather than
    starting a new one. Empty default means "fresh session." The Test
    Skill dialog passes the id captured from the previous run's JSON
    envelope (§7.46), giving the user multi-turn context within one
    open dialog.

    ``json_output`` (optional) appends ``--output-format json``, which
    makes ``claude`` emit a single JSON object instead of bare
    markdown. Used by the dialog only when continuing, because the
    session_id we need to capture lives inside that envelope. The
    plain-text default round-trips byte-for-byte with the pre-resume
    invocation shape so non-continuing runs are unaffected.

    ``extra_read_dirs`` (optional) emits a single ``--add-dir`` flag
    followed by every path, granting ``claude`` read access in those
    directories in addition to ``cwd``. The flag is variadic in the
    Claude CLI (``--add-dir <directories...>``) — one flag accepts
    multiple values. We emit one flag with N values rather than N
    flags with one value each: repeating the flag has commander.js
    version-dependent merge semantics, but variadic-with-many-values
    is the documented shape and unambiguous.

    A POSIX ``--`` separator is ALWAYS emitted before the positional
    prompt. Without it, the variadic collector on ``--add-dir`` would
    consume the prompt as another directory and Claude would exit 1
    with "path does not exist: <prompt-text>" within 3 seconds. The
    ``--`` is cheap insurance even when ``--add-dir`` is absent — a
    prompt starting with ``-`` would otherwise be misparsed as an
    option flag.

    The Test Skill dialog passes the selected skill's directory when
    the skill lives outside ``cwd`` — without it, prompts that
    reference ``<skill>/SKILL.md`` by path can't Read it (Read
    defaults to cwd only). Skipped when ``skip_permissions=True``
    since that flag bypasses the gate entirely, making per-directory
    grants redundant.

    ``skip_permissions`` (optional) appends
    ``--dangerously-skip-permissions``, which tells ``claude`` to
    bypass every tool-use confirmation prompt for this invocation.
    In interactive ``claude``, file writes / shell commands /
    network calls pop up a "Allow?" prompt and wait for the user's
    keystroke. In ``--print`` mode there's no human-in-the-loop, so
    those tools are denied by default; the flag is the documented
    escape hatch for batch / scripted use (and for the Test Skill
    window, where the user wants to see the skill actually do
    things). Anthropic chose the long ``dangerously-`` prefix to
    discourage casual leaving-it-on; we honor that by requiring an
    explicit opt-in checkbox per Test Skill window rather than
    making it a global preference.

    Prompt is its own argv element. Qt's ``QProcess.start(program,
    args)`` handles per-platform quoting, so prompts containing shell
    metacharacters (quotes, backticks, dollar signs, newlines) need no
    escaping on our side. Avoid joining argv into a shell string —
    that's where injection bugs live."""
    exe = find_claude_executable() or CLAUDE_EXECUTABLE
    argv: list[str] = [exe, "--print"]
    if model:
        argv += ["--model", model]
    if session_id:
        argv += ["--resume", session_id]
    if json_output:
        argv += ["--output-format", "json"]
    if skip_permissions:
        argv += ["--dangerously-skip-permissions"]
    if extra_read_dirs:
        # Variadic flag: one --add-dir, every path follows. Repeated
        # flag emission is version-dependent in commander.js; this
        # shape matches the documented usage exactly.
        argv.append("--add-dir")
        for d in extra_read_dirs:
            argv.append(str(d))
    # POSIX `--` terminates option parsing. Required because the
    # --add-dir variadic above would otherwise eat the prompt as a
    # second directory, exit-1ing claude with "path does not exist:
    # <prompt-text>" in ~3 seconds. Emitted unconditionally as cheap
    # insurance even when --add-dir is absent.
    argv.append("--")
    argv.append(prompt)
    return argv


def parse_claude_json_envelope(
    stdout: str,
) -> tuple[str, str | None, bool]:
    """Parse the JSON envelope emitted by ``claude --output-format json``.

    Returns ``(response_text, session_id_or_None, is_error)``:

    * ``response_text`` — the assistant's reply, extracted from the
      ``result`` field. Falls back to the raw ``stdout`` verbatim when
      parsing fails so the user always sees *something* and can debug
      from the Raw Output tab.
    * ``session_id`` — the conversation id to feed back as
      ``--resume`` on the next turn. ``None`` if absent or non-string.
    * ``is_error`` — claude's own ``is_error`` flag; True means the
      response represents an error condition (rate limit, permission
      issue, etc.). Caller is expected to label the rendered output
      accordingly so a downstream "looks like a normal answer" mistake
      is avoided.

    Pure function — no Qt, no logging, no side effects. Kept in the
    domain layer so the dialog's UI code can stay focused on widget
    plumbing."""
    try:
        data = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return stdout, None, False
    if not isinstance(data, dict):
        return stdout, None, False

    result = data.get("result")
    if not isinstance(result, str) or not result:
        # Some failure modes (auth error, rate limit) put the
        # message in `error` / `message` instead. Fall through to
        # the raw stdout so the user sees claude's own wording
        # instead of an empty pane.
        result = stdout

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = None

    is_error = bool(data.get("is_error", False))
    return result, session_id, is_error


def claude_env_overrides(api_key: str = "") -> dict[str, str]:
    """Return env-var overrides to merge into ``os.environ`` when
    launching ``claude``.

    Currently this is just ``ANTHROPIC_API_KEY`` when ``api_key`` is
    non-empty — the standard Anthropic env var name; ``claude`` reads
    it the same way the SDK does. Empty input returns an empty dict so
    callers can `dict.update`/`{**env, **overrides}` unconditionally
    without an empty-key footgun.

    Kept as a pure function rather than reaching into app_settings
    directly so the domain layer stays settings-free (the UI layer
    reads settings and passes the value in). Mirrors the pattern in
    :func:`build_claude_command`."""
    if not api_key:
        return {}
    return {"ANTHROPIC_API_KEY": api_key}


def working_directory_for(skill: Skill) -> Path:
    """Return the directory ``claude`` should run in for ``skill``.

    * **Project skills** — run in the project root (the folder
      containing ``.claude/``) so any project-scoped settings
      (permissions, project-local ``settings.local.json``) take
      effect. The canonical layout is ``<root>/.claude/skills/<name>``,
      so ``parents[2]`` is the root.
    * **Global / Plugin skills** — run in ``Path.home()``. There's no
      project context to honor, and home is a safe, predictable cwd
      that doesn't accidentally pick up an unrelated project's
      ``.claude`` directory.

    ``IndexError`` fallback covers a malformed skill path (shouldn't
    happen for a scanner-discovered skill, but defensive); fall back
    to home rather than raising — running the test in the wrong cwd
    is a degraded experience, but crashing the dialog is worse."""
    if skill.type == SkillType.PROJECT:
        try:
            return skill.path.parents[2]
        except IndexError:
            return Path.home()
    return Path.home()
