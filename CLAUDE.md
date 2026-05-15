# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical reference

`DESIGN.md` is the single source of truth for architecture, technology choices, the three-source skill discovery model, the iteration log of bugs and their fixes, and cross-cutting patterns. **Read `DESIGN.md` before making non-trivial changes** — it documents *why* several non-obvious choices look the way they do (Qt enum strictness, `modificationChanged` vs. `textChanged`, content-based dirty, lazy `QFileSystemModel` attachment, etc.).

The notes below are a quick orientation; they do not replace `DESIGN.md`.

## Run / develop

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

There is **no test suite, linter, or build step**. The repo's built-in "validation" is a syntax check via `python -c "import ast; ast.parse(open('<file>', encoding='utf-8').read()); print('OK')"` (see `.claude/settings.local.json`). End-to-end verification is by launching the GUI.

Dependencies are deliberately minimal: PySide6 (Qt bindings, LGPL) and PyYAML.

## Architecture in one paragraph

Three-pane PySide6 desktop app: skill list (left) → file tree (middle) → editor + markdown preview (right). `MainWindow` owns the only `SkillScanner` and routes Qt signals between panels. Each panel exposes `clear()` and emits signals upward; panels never reach into each other directly. Skills come from three on-disk sources — Global (`~/.claude/skills`), Plugin (`~/.claude/plugins/marketplaces/*/plugins/*/skills`), and Project (`<root>/**/.claude/skills`, recursive with depth + ignore-list guards).

## Layering rule (load-bearing)

`claude_skills_manager/models.py`, `scanner.py`, and `skill_md.py` **must remain Qt-free**. They are imported from a plain Python script and form the unit-testable seam. UI code (`ui/*.py`) depends on the domain modules; the reverse direction is forbidden.

## Conventions worth preserving

These come from concrete bugs documented in `DESIGN.md` §7. Re-introducing them is easy if you don't know the history:

- **Construct panels before wiring toolbar signals.** `QCheckBox.setChecked(True)` emits `toggled` synchronously, firing slots that reference `self.skill_list` etc. — those attributes must already exist. See `_build_ui` ordering in `ui/main_window.py`.
- **Use named Qt enums, not integers.** PySide6 (unlike PyQt5) does not coerce `0` to `Qt.AscendingOrder`. Always pass the enum.
- **Dirty state is content-based, not flag-based.** `EditorPanel` stores a `_pristine_text` snapshot and computes `dirty = buffer != pristine` on `textChanged`. Do not switch back to `QTextDocument.modificationChanged` — it can't detect "type the file back to original," and the previous `textChanged` noise from `QSyntaxHighlighter` rehighlights is not actually a problem under content-compare (formats change, plain text doesn't).
- **Cross-panel coordination via signals only.** `skill_selected`, `file_activated`, `file_saved` flow up to `MainWindow`; method calls flow down. Adding a direct panel-to-panel reference couples them and breaks the per-panel `clear()` orchestration in `MainWindow.refresh()`.
- **Lazy `QFileSystemModel` attachment.** `FileTreePanel.tree` has no model until first `show_directory()`. An attached-but-unrooted `QTreeView + QFileSystemModel` shows the Windows drive list, which is wrong for "no skill selected."
- **Description tab ≠ Editor tab — intentionally.** Description renders the *selected skill's* `SKILL.md` (markdown, frontmatter stripped, name + description as header). Editor shows whatever *file* the user clicked, raw. Re-render Description on `tabs.currentChanged`, not on every keystroke.

## Project-scan knobs

In `scanner.py`:

- `MAX_SCAN_DEPTH = 8` — defensive cap so pointing the project root at `C:\` doesn't hang.
- `IGNORED_DIRS` — vendored / build / cache directories to skip. **Always allow descending into `.claude` itself**, even though it's a dotfile.
- `dirnames[:] = [...]` (slice-assign) is the only correct way to prune `os.walk` in place — reassigning `dirnames = ...` silently does nothing.
- Skills are deduplicated by absolute resolved `Skill.path`.

## Persistence

UI state (project root, type-filter checkboxes, geometry, splitter state) is stored via `QSettings` under organization `ClaudeSkillsManager` / app `ClaudeSkillsManager`. On Windows that's the registry under `HKCU\Software\ClaudeSkillsManager\ClaudeSkillsManager`.

## What's intentionally absent

- No tests, no CI, no linter, no formatter config.
- No global `app.setStyleSheet` — stylesheets are scoped per widget (see `_TAB_STYLE` in `editor_panel.py`, `_SKILL_LIST_STYLE` in `skill_list.py`).
- No external markdown library — `QTextBrowser.setMarkdown()` covers the use case.
- No external syntax-highlighting library — hand-rolled `QSyntaxHighlighter` rules in `ui/syntax.py` for `.py`, `.json`, `.md` only. Other extensions render plain.
