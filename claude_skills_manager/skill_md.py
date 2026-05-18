"""Parse SKILL.md frontmatter and a fallback description."""
from __future__ import annotations

import re
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_skill_md(path: Path) -> tuple[dict, str]:
    """Return (metadata_dict, description_string) by reading ``path``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    return parse_skill_md_text(text)


def parse_skill_md_text(text: str) -> tuple[dict, str]:
    """Same as :func:`parse_skill_md` but operates on an in-memory string,
    so the preview can re-parse the editor buffer without saving to disk.

    YAML frontmatter is preferred; if absent or malformed we degrade to a
    naive ``key: value`` parser, then to the first non-heading paragraph
    of the body.
    """
    metadata: dict = {}
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        metadata = _parse_yaml_block(match.group(1))
        body = text[match.end():]

    description = metadata.get("description") if isinstance(metadata.get("description"), str) else ""
    if not description:
        description = _first_paragraph(body)
    return metadata, description


def _parse_yaml_block(block: str) -> dict:
    try:
        import yaml  # PyYAML is in requirements; this is the happy path
        loaded = yaml.safe_load(block)
        return loaded if isinstance(loaded, dict) else {}
    except ImportError:
        return _naive_parse(block)
    except Exception:
        return _naive_parse(block)


def _naive_parse(block: str) -> dict:
    out: dict = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def strip_frontmatter(md: str) -> str:
    """Drop a leading YAML frontmatter block — and any orphan bare ``---``
    lines that immediately follow it — so the rendered preview shows
    only the prose body.

    The "and orphan ``---`` lines" tail is load-bearing. Some
    SKILL.md files in the wild ship with a *doubled* terminator
    (``---\\nyaml\\n---\\n---``) — an authoring slip where the writer
    typed three dashes twice at the end of the frontmatter. The
    ``code-review`` skill in the ``idm-agent-skills`` marketplace is
    one such file. Without eating the orphan, Qt's CommonMark parser
    reads the leftover ``---`` as the *opener* of a new YAML block and
    scans ahead for a closer — finding one inside a triple-backtick
    code block further down the file, which hides every line in
    between (most of the document). See §7.28 for the parser-composition
    failure mode this guards against.

    The loop also handles a third or fourth terminator gracefully if
    anyone manages to triple-tap; bare ``---`` lines render as
    horizontal rules either way, but stripping them at the head keeps
    the rendered output starting with a real heading.

    Lives in :mod:`skill_md` (rather than the previous home in
    ``editor_panel``) so both the editor panel's Preview tab and the
    test dialog's Description tab share one implementation — the
    §7.28 defense is a meaningful behavior, not boilerplate to
    duplicate."""
    if not md.startswith("---"):
        return md
    end = md.find("\n---", 3)
    if end == -1:
        return md
    next_newline = md.find("\n", end + 4)
    if next_newline == -1:
        return ""
    body = md[next_newline + 1:]
    # Eat any bare "---" lines that follow the frontmatter close
    # (blanks between them are fine — we just want to skip ahead to the
    # first non-"---" line of actual content).
    while True:
        peeked = body.lstrip("\r\n")
        line_end = peeked.find("\n")
        line = peeked if line_end == -1 else peeked[:line_end]
        if line.rstrip("\r").strip() != "---":
            break
        if line_end == -1:
            return ""
        body = peeked[line_end + 1:]
    return body


def estimated_token_count(text: str, *, chars_per_token: int = 4) -> int:
    """Rough token-count estimate (chars / ``chars_per_token``, rounded up).

    Claude's tokenizer isn't bundled locally, so we use the widely-cited
    chars-per-token approximation. The default of 4 chars/token is the
    standard fallback for mixed prose + code; round-up keeps the estimate
    slightly conservative for context-budget planning."""
    if not text:
        return 0
    return (len(text) + chars_per_token - 1) // chars_per_token


def _first_paragraph(text: str, limit: int = 500) -> str:
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
