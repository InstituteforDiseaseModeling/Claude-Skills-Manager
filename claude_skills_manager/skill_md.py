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
