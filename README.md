# Claude Skills Manager

A Python desktop GUI for browsing, editing, enabling/disabling, and
testing **Claude Code skills** from the three on-disk sources Claude
Code itself consults:

| Source  | Location |
|---------|----------|
| Global  | `~/.claude/skills/<skill>/SKILL.md` |
| Plugin  | `~/.claude/plugins/marketplaces/<m>/.../<plugin>/skills/<skill>/SKILL.md` |
| Project | `<project-root>/**/.claude/skills/<skill>/SKILL.md` |

Built with PySide6. Cross-platform in principle; primary development
target is Windows.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Requires Python 3.10+ (uses `from __future__ import annotations`, PEP
604 union syntax, and `Path.is_relative_to`).

Dependencies are deliberately minimal — `PySide6` (Qt bindings, LGPL)
and `PyYAML` for frontmatter parsing.

## What it does

Three-pane layout:

```
┌────────────────┬──────────────────────┬──────────────────────────────────┐
│ Skill list     │ File tree            │ Description / Editor / Preview   │
│                │                      │                                  │
│ Global (N)     │ ▼ skill-name/        │ Renders SKILL.md as markdown;    │
│ Project (N)    │    SKILL.md          │ opens any clicked file in an     │
│ Plugin  (N)    │    scripts/run.py    │ editor with .py / .json / .md    │
│                │    README.md         │ syntax highlighting. Images      │
│                │                      │ open in a zoom/pan viewer.       │
└────────────────┴──────────────────────┴──────────────────────────────────┘
```

- **Discovery.** Scans all three sources on startup and on **Refresh**.
  The Project scan is depth-limited (`MAX_SCAN_DEPTH = 8`) and skips
  the usual vendored/build directories — pointing it at `C:\` will not
  hang.
- **Filter & search.** Toolbar checkboxes filter by type (Global /
  Project / Plugin) and state (Enabled / Disabled); the search box
  does a live name-substring filter on top.
- **Enable / Disable.** Right-click a Global or Project skill row →
  toggles `skillOverrides[<name>] = "off"` in the scope's
  `.claude/settings.local.json`. Plugin-skill enablement is read-only
  in the GUI; use `/plugin` in Claude Code itself.
- **Editor.** `Ctrl+S` saves. Dirty state is content-based: typing a
  file back to its original contents un-dirties the buffer.
- **Test Skill (`Ctrl+T`).** Modeless per-skill dialog that shells out
  to `claude -p` and streams the response. Has a **Working Directory**
  field (the project context Claude runs in) and a **Trust Directory**
  field (emitted as `--add-dir` so Claude can read the selected skill
  even when it lives outside the working directory — e.g. a Global or
  Plugin skill).
- **Persistence.** Project root, filter checkboxes, window geometry,
  and splitter state survive across launches via `QSettings`.

## Layout

```
Claude_Skills_Manager_GUI/
├── main.py                     entry point
├── requirements.txt
├── CLAUDE.md                   guidance for Claude Code working in this repo
├── DESIGN.md                   architecture, decisions, iteration log
└── claude_skills_manager/
    ├── models.py               Skill, SkillType (Qt-free)
    ├── scanner.py              three-source discovery + state pass
    ├── skill_md.py             frontmatter parser + token estimator
    ├── skill_settings.py       skillOverrides + enabledPlugins read/write
    ├── skill_introspect.py     examples extraction + `claude -p` argv builder
    ├── claude_trust.py         ~/.claude.json trust-folder writer
    └── ui/                     PySide6 widgets (QTreeWidget, editor, dialogs)
```

**Layering rule.** `models.py`, `scanner.py`, `skill_md.py`,
`skill_settings.py`, `skill_introspect.py`, and `claude_trust.py` are
Qt-free. The UI depends on them; not the reverse. See `CLAUDE.md` for
the full list of invariants worth preserving.

## Tests, CI, etc.

There is no test suite, linter, formatter, or build step in this
repository. The lightweight validation gate is `python -c "import ast;
ast.parse(open('<file>', encoding='utf-8').read())"` for any file
that's been edited. End-to-end verification is by launching the GUI.

## More

- `DESIGN.md` — the canonical reference. Architecture, technology
  choices, the three-source discovery model, and a numbered iteration
  log of every bug encountered and how it was fixed. Read this before
  making non-trivial changes.
- `CLAUDE.md` — guidance for Claude Code when working in this repo;
  short orientation plus a list of load-bearing conventions.
