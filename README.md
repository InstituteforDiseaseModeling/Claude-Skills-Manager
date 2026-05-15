# Claude Skills Manager

A desktop GUI for discovering, browsing, and editing **Claude Code skills**
on your machine. Three-pane layout: skill list → file tree → editor with
live markdown preview.

Built with PySide6. Runs on Windows, macOS, and Linux.

---

## Why

Claude Code skills live in three places on disk, and there's no single
place to see what you have, what's enabled, or what's inside each one:

| Source      | Location                                                              |
| ----------- | --------------------------------------------------------------------- |
| **Global**  | `~/.claude/skills/`                                                   |
| **Plugin**  | `~/.claude/plugins/marketplaces/*/plugins/*/skills/`                  |
| **Project** | `<your-project>/**/.claude/skills/` (recursive)                       |

This app scans all three, groups them by source, and lets you read or edit
the `SKILL.md` (and its supporting files) without leaving the window.

## Features

- **Three-source discovery** — Global, Plugin, and Project skills in one list.
- **Filter by source and state** — toggle Global/Project/Plugin, Enabled/Disabled.
- **Search** — quick filter by skill name or description.
- **Markdown preview** — `SKILL.md` rendered with frontmatter stripped.
- **In-app editor** — `QPlainTextEdit` with line numbers and lightweight syntax
  highlighting for `.py`, `.json`, and `.md`. Content-based dirty detection
  (no false positives from re-highlighting).
- **Skill metadata panel** — size, modification time, and an approximate
  token count for the selected `SKILL.md`.
- **Enable / Disable** project skills via `skillOverrides` in
  `.claude/settings.json`.
- **Persistent layout** — window geometry, splitter positions, project root,
  and filter checkboxes are saved between runs (`QSettings`).

## Install & run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

On macOS / Linux, activate the venv with `source .venv/bin/activate`.

### Install as a package

`pyproject.toml` is configured so you can install the app and get a
GUI launcher shim:

```powershell
pip install .
claude-skills-manager
```

Requires Python 3.10 or newer.

## How to use

1. **Choose a project root** from the toolbar if you want Project skills to
   appear. Global and Plugin skills load automatically.
2. **Click a skill** in the left panel — the middle panel roots its file
   tree to that skill, and the right panel switches to the **Description**
   tab with the rendered `SKILL.md`.
3. **Click a file** in the middle tree — the right panel's **Editor** tab
   loads it. Image files (`.png`, `.jpg`, …) open in a separate viewer.
4. **Edit, Save, Revert** — the title bar dot (●) indicates unsaved changes.
5. **Enable / Disable** a project skill from the metadata panel below the
   file tree.

## Architecture

Three-pane PySide6 desktop app. `MainWindow` owns the only `SkillScanner`
and routes Qt signals between the three panels — panels never reach into
each other directly. Domain modules (`models.py`, `scanner.py`,
`skill_md.py`) are kept **Qt-free** so they're unit-testable from a plain
script.

```
main.py
└── claude_skills_manager/
    ├── models.py          Skill, SkillType         (Qt-free)
    ├── scanner.py         3-source discovery       (Qt-free)
    ├── skill_md.py        SKILL.md parser          (Qt-free)
    ├── skill_settings.py  skillOverrides r/w       (Qt-free)
    └── ui/
        ├── main_window.py     toolbar + signal routing
        ├── skill_list.py      left panel
        ├── file_tree.py       middle panel
        ├── skill_info_panel.py metadata + Enable/Disable
        ├── editor_panel.py    Description / Editor tabs
        ├── code_editor.py     QPlainTextEdit + line numbers
        ├── syntax.py          .py / .json / .md highlighters
        └── …
```

See [`DESIGN.md`](DESIGN.md) for the full architectural rationale, the
three-source discovery model, the iteration log of bugs and fixes, and the
non-obvious Qt conventions (named enums, content-based dirty, lazy
`QFileSystemModel` attachment, …).

For agent / AI-assistant guidance when working in this repo, see
[`CLAUDE.md`](CLAUDE.md).

## Development notes

- **No test suite, linter, or formatter** — by design, to keep the surface
  small. Quick syntax validation: `python -c "import ast; ast.parse(open('<file>', encoding='utf-8').read())"`.
- **End-to-end verification is by launching the GUI.**
- **Dependencies are deliberately minimal** — PySide6 (LGPL) and PyYAML.
  No external markdown library (`QTextBrowser.setMarkdown()` handles it).
  No external syntax-highlighting library (hand-rolled `QSyntaxHighlighter`).
- **Layering rule** — `models.py`, `scanner.py`, `skill_md.py` must remain
  Qt-free. UI depends on domain, never the reverse.

## License

MIT.
