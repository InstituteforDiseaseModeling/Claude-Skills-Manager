"""Parse the packaged ``ai_tools.md`` markdown table into AITool records.

Qt-free domain module. The Resource menu in the UI layer calls
``load_ai_tools()`` once at startup and builds one sub-menu entry per
returned record. Parse failures are swallowed and logged — an empty
list yields a disabled-placeholder menu, never a crash.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger("ai_tools")

# Resource lives at ``claude_skills_manager/resources/ai_tools.md`` —
# next to the package's other domain modules, not under ``ui/``, because
# this module must stay Qt-free per the layering rule in CLAUDE.md.
_DEFAULT_RESOURCE = Path(__file__).parent / "resources" / "ai_tools.md"


@dataclass(frozen=True)
class AITool:
    """One row of the AI Tools table."""
    name: str
    main_url: str
    docs_url: str
    summary: str


def load_ai_tools(path: Path | None = None) -> list[AITool]:
    """Read and parse the packaged AI tools table.

    Returns an empty list on any failure (missing file, malformed
    table, no data rows). The first markdown table in the file is
    consumed; everything after it — "Recommended Documentation
    Portals" sections, "Notes", etc. — is ignored by design.
    """
    src = path if path is not None else _DEFAULT_RESOURCE
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _logger.warning("ai_tools resource unreadable at %s: %s", src, exc)
        return []
    return _parse_ai_tools_text(text)


def _parse_ai_tools_text(text: str) -> list[AITool]:
    """Parse markdown body into a list of AITool records.

    Strategy: scan for the first ``| ... |`` header row followed by a
    ``| --- |`` separator. Treat every subsequent ``|``-prefixed line
    as a data row until a non-table line (blank or ``#``-heading)
    ends the table. Defensive at every step — a single malformed row
    is skipped, not fatal.
    """
    lines = text.splitlines()
    header_idx = _find_table_header(lines)
    if header_idx is None:
        _logger.warning("ai_tools: no markdown table found")
        return []

    headers = _split_row(lines[header_idx])
    name_col = _find_col(headers, ("ai tool", "tool", "name"))
    main_col = _find_col(headers, ("main website", "website", "main"))
    docs_col = _find_col(headers, ("documentation", "docs", "developer"))
    summary_col = _find_col(headers, ("summary", "description"))

    if None in (name_col, main_col, docs_col, summary_col):
        _logger.warning(
            "ai_tools: required columns missing in header %r", headers)
        return []

    tools: list[AITool] = []
    # Data rows start two lines below the header (header + separator).
    for line in lines[header_idx + 2:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            break
        if not stripped.startswith("|"):
            break
        cells = _split_row(line)
        # Defensive: row narrower than expected → skip, don't crash.
        if max(name_col, main_col, docs_col, summary_col) >= len(cells):
            continue
        name = cells[name_col].strip()
        main_url = cells[main_col].strip()
        if not name or not main_url:
            continue
        tools.append(AITool(
            name=name,
            main_url=main_url,
            docs_url=cells[docs_col].strip(),
            summary=cells[summary_col].strip(),
        ))
    return tools


def _find_table_header(lines: list[str]) -> int | None:
    """Return the index of the first table header line (the one
    above a ``| --- |``-style separator), or None."""
    for i in range(len(lines) - 1):
        if lines[i].lstrip().startswith("|") and _is_separator(lines[i + 1]):
            return i
    return None


def _is_separator(line: str) -> bool:
    """Markdown table separator row: pipes plus cells of dashes
    (with optional ``:`` alignment markers and whitespace)."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    cells = _split_row(line)
    if not cells:
        return False
    for cell in cells:
        compact = cell.strip().replace(":", "").replace(" ", "")
        if not compact or set(compact) != {"-"}:
            return False
    return True


_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _split_row(line: str) -> list[str]:
    """Split a ``| a | b | c |`` row into ``["a", "b", "c"]``,
    discarding the leading/trailing empties from the outer pipes.

    Recognises the ``\\|`` escape that ``save_ai_tools`` emits when a
    cell contains a literal pipe. The negative-lookbehind splits only
    on un-escaped pipes; the per-cell unescape then collapses ``\\|``
    back to ``|`` so the value round-trips through save→load."""
    parts = _UNESCAPED_PIPE.split(line)
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.replace(r"\|", "|") for p in parts]


def _find_col(headers: list[str], keywords: tuple[str, ...]) -> int | None:
    """Return the index of the first header cell whose lowercase
    text contains any of ``keywords``. Lets the parser tolerate
    "Official Documentation / Developer Docs" vs plain "Docs"
    without hard-coding the exact source-file phrasing."""
    for idx, cell in enumerate(headers):
        lowered = cell.strip().lower()
        if any(kw in lowered for kw in keywords):
            return idx
    return None


# ----------------------------------------------------------------- save


_TABLE_HEADER = (
    "| AI Tool | Main Website | Official Documentation / Developer Docs"
    " | Summary Description |"
)
_TABLE_SEPARATOR = "| --- | --- | --- | --- |"


def save_ai_tools(tools: list[AITool], path: Path | None = None) -> None:
    """Serialize ``tools`` as a compact markdown table at ``path``.

    Writes UTF-8 atomically via tmp-file + os.replace so a crash
    mid-write cannot leave a half-empty resource file. The header
    text is fixed (matches the source distribution) and the parser's
    keyword-based column finder absorbs any subsequent header
    rewording, so the two sides stay decoupled.
    """
    dest = path if path is not None else _DEFAULT_RESOURCE
    lines = [_TABLE_HEADER, _TABLE_SEPARATOR]
    for tool in tools:
        lines.append(
            f"| {_escape_cell(tool.name)}"
            f" | {_escape_cell(tool.main_url)}"
            f" | {_escape_cell(tool.docs_url)}"
            f" | {_escape_cell(tool.summary)} |"
        )
    body = "\n".join(lines) + "\n"
    # Atomic write: write to <name>.tmp in the same directory, then
    # os.replace to swap. Same-directory rename is atomic on Windows
    # and POSIX; cross-directory is not, hence the sibling tmp file.
    import os
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, dest)


def _escape_cell(value: str) -> str:
    """Make ``value`` safe to inline in a markdown table cell.

    Two rules: ``|`` would close the cell early → escape as ``\\|``
    (the parser's ``_split_row`` recognises the escape and collapses
    it back); newlines would break the row layout → flatten to a
    single space. Round-tripping is lossy for newlines — intentional,
    since markdown tables don't natively support them.
    """
    flat = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return flat.replace("|", r"\|").strip()
