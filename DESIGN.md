# Claude Skills Manager — Design & Implementation Log

A Python desktop GUI that scans, browses, and edits **Claude Code skills** from
three on-disk sources (Global, Plugin, Project), with a markdown-rendered
description preview and an in-app editor.

This document captures the design as built, the technology decisions that got
us there, and the iterative bug-fix history that shaped the final UX.

---

## 1. Technology Choice

| Framework | Pros | Cons | Verdict |
|---|---|---|---|
| **Tkinter** | Bundled with Python, zero deps | No native tree+editor combo, no syntax highlighting, dated look, painful for split-panel apps with markdown preview | Underpowered |
| **PySide6 / PyQt6** | Native widgets; rich `QTreeView` + `QFileSystemModel` + `QPlainTextEdit` + `QSyntaxHighlighter` + `QTextBrowser.setMarkdown()` built in; excellent splitter/dock support; fast; cross-platform | Larger install (~70 MB); steeper API surface | **Best fit** |
| **Electron + Flask/FastAPI** | Web tech (React, Monaco editor) gives the richest UI | Bundles Chromium per app (~150 MB); two-language stack; IPC overhead | Overkill |
| **Tauri (Rust + webview)** | Tiny binaries, modern web UI | Requires Rust toolchain; user wants Python | Out of scope |
| **Textual** | Beautiful TUI, pure Python | Terminal-only — no native file dialog, no rendered markdown, weak for big editors | Wrong medium |

**Final choice: PySide6** (LGPL — friendlier license than PyQt6's GPL,
identical API). Every spec requirement maps directly to a built-in Qt class:

- Skill list → `QTreeWidget` (groupable, supports per-item user data)
- Recursive file tree → `QFileSystemModel` + `QTreeView` (free file-watching, lazy)
- Markdown preview → `QTextBrowser.setMarkdown()` (Qt 5.14+, no extra deps)
- Code editor → `QPlainTextEdit` + custom `LineNumberArea` + `QSyntaxHighlighter`
- Persistent UI state → `QSettings`

> **Insight.** Picking PySide6 over Electron isn't about "Python good, JS bad" —
> it's about matching tool to surface area. This app is a tree-tree-editor
> split with filesystem I/O. Qt's model/view classes give that for free with
> native scrolling and watching; an Electron app would spend its first week
> reimplementing what Qt ships.

---

## 2. Architecture

```
Claude_Skills_Manager_GUI/
├── main.py                          # entry point
├── requirements.txt
└── claude_skills_manager/
    ├── models.py                    # Skill, SkillType (Qt-free)
    ├── scanner.py                   # SkillScanner — 3-source discovery + state pass
    ├── skill_md.py                  # SKILL.md frontmatter parser + token estimator
    ├── skill_settings.py            # skillOverrides + enabledPlugins read/write
    └── ui/
        ├── _icons.py                # general-purpose UI icons (search magnifier, …)
        ├── _styles.py               # shared QSS constants (BUTTON_STYLE)
        ├── app_icon.py              # programmatic 3-shapes composite logo
        ├── win32_taskbar.py         # Win32: per-window AppUserModelID + icon resource
        ├── main_window.py           # QMainWindow, Type+State filter toolbar, signals
        ├── skill_list.py            # left panel — grouped tree, badges, disambiguation
        ├── file_tree.py             # middle-top — QFileSystemModel-backed tree
        ├── skill_info_panel.py      # middle-bottom — SKILL.md stats + Enable/Disable
        ├── editor_panel.py          # right panel — Skill Description + Editor + (conditional) Preview tabs
        ├── image_dialog.py          # modal QGraphicsView image viewer (zoom/pan)
        ├── code_editor.py           # QPlainTextEdit + line numbers
        └── syntax.py                # Python / JSON / Markdown highlighters
```

**Layering rule.** UI only depends on `models` and the domain modules
(`scanner`, `skill_md`). The domain modules have **zero Qt imports**, which
means the discovery logic is unit-testable from a plain script and could be
reused by a CLI or web frontend later.

> **Insight.** Same separation Qt's own model/view architecture pushes — data
> on one side, presentation on the other. Resist letting UI code reach into
> filesystem logic and vice versa.

---

## 3. UI/UX Layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Project root: …  [Choose…]  │  [Refresh]                                     │
│ Type: [✓Global][✓Project][✓Plugin]  State: [✓Enabled][✓Disabled]  [Search… ]│
├────────────────┬──────────────────────┬──────────────────────────────────────┤
│ Skill list     │ File tree            │ ┌─Description──Editor───────────────┐│
│                │ ▼ skill-name/        │ │  ● C:\…\skill.py    [Revert][Save]││
│ Global (12)    │    SKILL.md          │ │   1│ from pathlib import Path     ││
│   pdf-fill     │    ▼ scripts/        │ │   2│                              ││
│   docx-toolkit │       run.py         │ │   3│ def load(p):                 ││
│ Project (3)    │    README.md         │ │   4│     return p.read_text()     ││
│   onboarding   │ ──── SKILL.md ────── │ │                                   ││
│ Plugin (8)     │ Size: 2.3 KB         │ │                                   ││
│                │ Modified: 2026-05-07 │ │                                   ││
│                │ Tokens (≈): 580      │ │                                   ││
└────────────────┴──────────────────────┴──────────────────────────────────────┘
            Status bar: Loaded 23 skills • Project • C:\proj\.claude\…
```

**Interaction flow.**
- Selecting a skill on the left → file tree roots to that skill's folder, the
  Description tab renders `SKILL.md`.
- Clicking a file in the middle tree → image extensions (`.png`/`.jpg`/
  `.svg`/…) open in a modal viewer with Ctrl+wheel zoom and drag-pan;
  text files open in the editor tab with syntax highlighting set by
  extension, **Ctrl+S** saves.
- Leading "● " on the file label indicates dirty state.
- Type and State checkboxes filter live (intersection); the search box does
  live name-substring filter on top of both.
- Right-click a skill row → copy path / SKILL.md path / file:// URL, open in
  Explorer, or Enable / Disable (Disable writes `skillOverrides[name] = "off"`
  to `<scope>/.claude/settings.local.json`; Enable removes the entry).
- Window geometry, project root, and toggle states persist via `QSettings`.

---

## 4. Data Model

```python
class SkillType(str, Enum):
    GLOBAL  = "Global"
    PROJECT = "Project"
    PLUGIN  = "Plugin"

@dataclass
class Skill:
    name: str               # frontmatter `name:` or folder name
    path: Path              # absolute path to skill folder (dedup key)
    type: SkillType
    description: str        # frontmatter `description:` or first paragraph
    skill_md_path: Path     # convenience pointer to SKILL.md
    metadata: dict          # full parsed YAML frontmatter
    state: str              # "on" / "name-only" / "user-invocable-only" /
                            # "off" — populated by scanner from skillOverrides;
                            # synthesized "plugin-off" for plugin skills whose
                            # owning plugin is disabled
    plugin_id: str | None   # "<plugin>@<marketplace>" for plugin skills,
                            # used to look up enabledPlugins; None otherwise
```

The file tree is rendered lazily via `QFileSystemModel` — no need to
materialise a `FileNode` graph, which keeps memory flat for skills with
thousands of files.

---

## 5. Three-Source Discovery

| Source | Pattern |
|---|---|
| Global  | `~/.claude/skills/<skill>/SKILL.md` |
| Plugin  | `~/.claude/plugins/marketplaces/<m>/.claude-plugin/marketplace.json` → resolved plugin folders → `<plugin>/skills/<skill>/SKILL.md` (manifest-driven; legacy folder walk as fallback — see §7.17 for the three layouts in the wild) |
| Project | `<root>/**/.claude/skills/<skill>/SKILL.md` (recursive, depth-limited) |

**Project scan hardening.**
- Depth limit (`MAX_SCAN_DEPTH = 8`) so pointing at `C:\` doesn't hang the UI.
- Ignored dirs: `.git`, `node_modules`, `venv`, `.venv`, `__pycache__`, `dist`,
  `build`, `.idea`, `.vscode`, `target`, `.next`, `.tox`, `site-packages`,
  `.pytest_cache`, `.mypy_cache`, `.cache`, `.gradle`.
- All other dotfile dirs skipped, **except** `.claude` itself.
- Once a `<x>/.claude/skills` directory is found, its children aren't recursed
  (a skill folder isn't itself a skill holder).
- Results deduplicated by absolute path.

> **Insight.** `dirnames[:] = [...]` (slice-assign) is the canonical way to
> prune `os.walk` in place. Reassigning `dirnames = ...` would silently do
> nothing — `os.walk` holds the original list reference.

---

## 5.1 State Resolution (Enabled / Disabled)

After scanning produces the raw list of skills, `SkillScanner._populate_states`
annotates each one with an effective visibility state. The lookup rules
differ across the three sources, and the asymmetry is *deliberate* — it
mirrors how Claude Code itself resolves visibility.

### Per-type rules

| Type | Settings file(s) read | Lookup key | Default if absent | Possible values |
|---|---|---|---|---|
| Plugin           | `~/.claude/settings.json` `enabledPlugins` (only — *not* `settings.local.json`) | `<plugin>@<marketplace>` (canonical names from `marketplace.json` — see §7.17) | **disabled** (synthesized `plugin-off`) | `on`, `plugin-off` |
| Global / Project | `<scope>/.claude/settings.json` AND `<scope>/.claude/settings.local.json` (`.local` wins on collision) `skillOverrides` | the skill's `name` | **enabled** (`on`) | `on`, `off`, `name-only`, `user-invocable-only` |

The `<scope>` for Global/Project is `skill.path.parents[1]` — i.e. the
`.claude/` directory containing the skill's `skills/` folder. Read scope
must match write scope for toggles to round-trip; see §7.15 for the bug
that forced this derivation.

`read_enabled_plugins` (in `skill_settings.py`) reads only the global
file because plugin enablement is a per-user-global concern, not a
per-project one. `read_overrides` merges the two settings files because
`skillOverrides` is intentionally a hybrid — `settings.json` for shared
team policy, `settings.local.json` for personal-only overrides, with
local winning.

### The opt-in / opt-out asymmetry

A skill absent from `enabledPlugins` is **disabled**. A skill absent
from `skillOverrides` is **enabled**. These are inverted defaults:

| Source | What "absent" means | Why |
|---|---|---|
| `enabledPlugins`  | disabled | Plugins ship code from third parties. Claude Code requires explicit opt-in per plugin to avoid auto-activating arbitrary code as soon as the marketplace is added. |
| `skillOverrides`  | enabled  | Global/Project skills are already opt-in *by virtue of being placed on disk* in `~/.claude/skills/` or `<project>/.claude/skills/`. The override exists only to *suppress* something the user already has. |

This is why the UI treats the two flows differently:
- Disabling a Global/Project skill writes `skillOverrides[name] = "off"`.
- Enabling a Global/Project skill *removes* the entry (absent == default
  == on; see §7.14 decision 2).
- Plugin skills can't be toggled in this UI per §7.14 decision 3 — the
  user must use `/plugin` to write to `enabledPlugins`.

### "Marketplace add" ≠ "plugin enable"

A subtle gotcha that surfaces every time a user installs a new
marketplace: **adding a marketplace via `/plugin marketplace add …`
makes plugins discoverable, not enabled.** The `experiment-dashboard`
plugin in `idm-agent-skills` is the canonical example — once the
marketplace is added, the GUI's manifest-driven scanner (§7.17)
correctly *finds* the plugin's skills, but each plugin still requires
a separate explicit `/plugin install …` (or equivalent step in the
`/plugin` UI) before its `plugin_id` lands in `enabledPlugins`.

So the GUI faithfully reports what's actually true: a row showing
`plugin-off` styling for a marketplace you just added isn't a bug — it
correctly reflects that Claude Code itself wouldn't run that plugin's
skills until you take the second step.

### State synthesis

The scanner computes states once per scan, never re-reading settings
during display:

```python
# scanner.py — _populate_states
for s in skills:
    if s.type == SkillType.PLUGIN:
        if s.plugin_id and enabled_plugins.get(s.plugin_id, False):
            s.state = STATE_ON
        else:
            s.state = STATE_PLUGIN_OFF
    else:
        s.state = overrides_for(s).get(s.name, STATE_ON)
```

Putting the state on the dataclass means panels just read `skill.state`
when rendering — no settings re-reads on every paint. After a successful
toggle write, `MainWindow._on_state_change_requested` updates the
single `Skill.state` value in place and calls `refresh_state(skill)`
on the panel; no full rescan needed for a one-skill change.

> **Insight.** The asymmetry between `enabledPlugins` and
> `skillOverrides` is the kind of detail that's easy to gloss past
> until it bites. Two structurally similar lookup tables, opposite
> defaults — and the GUI faithfully reflects both because the scanner
> obeys each table's own absence-semantics. Keeping the per-type rules
> in one place (this section + the table above) is what makes the
> rest of the code line up: panels never need to know "is this opt-in
> or opt-out?", they just trust `Skill.state`.

---

## 6. Implementation Plan (as executed)

1. Scaffold — `requirements.txt`, package layout, empty `MainWindow`.
2. Scanner — `scanner.py` + `skill_md.py`; testable independently.
3. Models wired — `MainWindow.refresh()` calls scanner, pushes to `SkillListPanel`.
4. Left panel — `SkillListPanel` with grouping, type filter, search.
5. Middle panel — `FileTreePanel` rooting `QFileSystemModel` on skill selection.
6. Right panel — Description tab (`QTextBrowser.setMarkdown`).
7. Right panel — Editor tab (`CodeEditor` + per-extension highlighter).
8. Save / dirty — `textChanged` → dirty flag → enables Save/Revert; close prompt.
9. Polish — `QSettings` persistence, status bar, binary-file guard, depth-limited recursion, ignored dirs.

---

## 7. Iteration Log

The initial implementation worked end-to-end on first run (`scan_all()` found
27 real skills). Subsequent iterations addressed bugs and UX refinements
discovered through manual use.

### 7.1 Toolbar built before panels referenced by it

**Symptom.** First launch crashed with
`AttributeError: 'MainWindow' object has no attribute 'skill_list'` from inside
`_build_toolbar`.

**Cause.** `_build_toolbar` connected checkbox `toggled` lambdas referencing
`self.skill_list`, then immediately called `cb.setChecked(True)`. In Qt,
`setChecked` emits `toggled` synchronously — so the lambdas fired during
construction, before `self.skill_list` had been created.

**Fix.** Construct panels first, then build the toolbar (`main_window.py`
in `_build_ui`).

> **Insight.** Qt signal/slot wiring is eager. Anything that sets initial
> widget state after `connect()` will fire those slots right then. Either
> connect AFTER setting initial state, or make sure every dependency
> referenced by a slot is constructed first.

### 7.2 PySide6 enum strictness

**Symptom.** `TypeError: 'PySide6.QtWidgets.QTreeView.sortByColumn' called with
wrong argument types: (int, int)`.

**Cause.** PyQt5 silently coerced `0` into `Qt.AscendingOrder`. PySide6 (and
PyQt6) require the actual enum value.

**Fix.** Pass `Qt.AscendingOrder` explicitly.

> **Insight.** Migration rule: prefer named Qt enums everywhere
> (`Qt.AlignRight`, `Qt.Horizontal`, `QFont.Bold`). Don't write the enum name
> as a comment when you can just pass it.

### 7.3 Phantom dirty flag from highlighter

**Symptom.** Switching between `.md` files prompted "Unsaved changes" even
when nothing had been edited.

**Cause.** Dirty was driven by `QPlainTextEdit.textChanged`. Attaching a new
`QSyntaxHighlighter` schedules a rehighlight, which (after our `_set_dirty(False)`
ran) flipped dirty back to True via `textChanged`.

**Fix.** Switched to `QTextDocument.modificationChanged` — Qt deliberately keeps
highlighter format changes out of the modified flag. Standard Qt pattern.

> **Insight.** Two "modified" concepts:
> - `QPlainTextEdit.textChanged` — fires on ANY content event, including
>   format-only changes from `QSyntaxHighlighter`.
> - `QTextDocument.modificationChanged(bool)` — backed by `setModified()`,
>   designed to track *user* edits.
>
> textChanged is a render-time signal, not a state-change signal.

### 7.4 Description tab vs. Editor tab — what's the difference?

**Question raised.** Tabs showed different content for the same `SKILL.md`.

**Answer.** Intentional asymmetry, both views answer different questions:

| Tab | Source | Rendering | Question |
|---|---|---|---|
| Description | Selected **skill**'s `SKILL.md` | Markdown rendered to HTML, frontmatter stripped | "What does this skill do?" |
| Editor | Whatever **file** you clicked | Raw text + syntax highlighting | "What's actually in this file?" |

The frontmatter is hidden from the Description because it's metadata for
Claude (skill-routing), not for the human reader.

> **Insight.** When two views could be identical but are intentionally
> different, document why — the reader's first instinct will be "this is a
> bug." The split mirrors how Claude itself reads SKILL.md: frontmatter for
> routing, body for instructions.

### 7.5 Show name + description in Description header

**Request.** Render both the skill's `name` and `description` at the top of
the Description tab, not just the body.

**Fix.** Compose the markdown as `[# name, > description, ---, body]`,
joined with blank lines.

> **Insight.** Using a list-and-join over `+=` concatenation isn't pure
> taste — Markdown REQUIRES blank lines between block elements. Without the
> blank line, `> foo` after `# heading` parses as continuation of the
> heading, not a blockquote. Same for `---`: must have blanks around it,
> otherwise it's read as a setext underline for the previous line.

### 7.6 Long lines didn't wrap in the Editor

**Cause.** `setLineWrapMode(QPlainTextEdit.NoWrap)` was set deliberately at
construction.

**Fix.** Switched to `WidgetWidth` + `WrapAtWordBoundaryOrAnywhere` so long
words (URLs, base64 blobs) still wrap rather than triggering horizontal
scroll.

> **Insight.** Two independent settings:
> - `setLineWrapMode` — does wrap happen at all, and where?
> - `setWordWrapMode` — given that we wrap, where does the break land within
>   a line?
>
> For prose: `WidgetWidth` + `WordWrap`. For code: `WidgetWidth` +
> `WrapAtWordBoundaryOrAnywhere` — keep words together when they fit, but
> break a 400-char token rather than hide it under horizontal scroll.

### 7.7 Toolbar polish

**Requests.**
1. Remove the confusing **Clear** button.
2. Right-align the search box.
3. Make the file tree blank by default (was showing `Windows (C:)`).

**Fixes.**
1. Removed the button + the now-unused `clear_project_root` method.
2. Inserted an expanding-policy `QWidget` spacer between the filter
   checkboxes and the search box.
3. Don't attach the `QFileSystemModel` to the `QTreeView` until the first
   skill selection. An unmodelled tree paints empty.

> **Insight.** A `QTreeView` attached to `QFileSystemModel` with no rootIndex
> shows the model's invisible root — on Windows that's "This PC" (drives).
> The clean fix isn't a sentinel path, it's lazy model attachment.
>
> For right-aligning in a `QToolBar`: there's no `addStretch()` like
> `QBoxLayout` has. The pattern is an empty `QWidget` with `Expanding`
> horizontal size policy.

### 7.8 Description tab didn't reflect unsaved edits to SKILL.md

**Symptom.** After modifying `SKILL.md` in the editor (without saving) and
switching to the Description tab, the preview still showed the on-disk content.

**Cause.** `show_skill()` was the only code path that rendered the
description, and it only ran when a *new* skill was selected. Switching tabs
didn't trigger a refresh.

**Fix.** Connected `tabs.currentChanged` to a re-render, and added a
source-picker: if the editor is currently holding **this skill's** SKILL.md,
read from the editor buffer; otherwise read from disk. Also exposed
`parse_skill_md_text(str)` so frontmatter edits in the editor reflect in the
header callout.

> **Insight.** Refresh on tab-change, not on every keystroke. Live-updating
> the preview as the user types would re-render markdown on every key for a
> passive view. Tab-change is the moment they're actually asking "what does
> this look like now?"

### 7.9 Subtle tab selection styling

**Request.** It was hard to tell which of Description / Editor was active.

**Fix.** Added a stylesheet on the `QTabWidget`:
- Inactive tabs: light grey (`#ececec`), dim text.
- Hover: lifts to near-white, darker text.
- Selected: white background, bold dark text, **3px blue underline**, with
  `margin-bottom: -1px` so the active tab visually merges with the panel.

> **Insight.** Stylesheets on `QTabWidget` work via `QTabBar::tab` selectors
> (the bar item, not the widget itself). Three pseudo-states do the work:
> `tab`, `tab:selected`, `tab:hover:!selected`. The `margin-bottom: -1px`
> trick lets the active tab "punch through" the pane border so the tab and
> panel below merge into one surface — that's the difference between "I can
> sort of tell" and "obviously selected."

### 7.10 Selected skill highlight + clear panes on Refresh

**Requests.**
1. Selected skill in the left panel should be visually distinct.
2. Refresh should clear the middle and right panes (no skill is selected
   after refresh).

**Fixes.**
1. Stylesheet on `QTreeWidget::item:selected` (blue + bold + white text), plus
   `QTreeWidget::item:selected:!active` for a softer-blue fallback when the
   tree loses focus. Without `:!active`, Windows dims the selection to grey.
2. Added `EditorPanel.clear()` and called both panel `clear()` methods from
   `MainWindow.refresh()` after `confirm_close()`.

> **Insight.** `_current_skill = None` happens BEFORE the rescan, not after.
> The old Skill object is about to be replaced — keeping the stale reference
> across the gap risks `_on_file_saved`'s identity check matching a stale
> object. Same pattern as nulling references before replacement in
> transactions / React reducers.

### 7.11 Discard didn't actually discard

**Symptom.**
1. Modify SKILL.md (don't save).
2. Switch skills → "Unsaved changes" prompt → click **Discard**.
3. Switch to a different skill → prompt fires AGAIN.

**Cause.** `confirm_close()` asked the user but never *applied* the discard.
The dirty flag stayed True, so the next switch re-prompted.

**Fix.** When the user clicks Discard, call `revert_current()` — this reloads
the file from disk, drops the in-memory edits, and clears the dirty flag in
one step.

> **Insight.** Classic dialog-result-vs-dialog-effect mistake. The function
> name `confirm_close` reads as "ask AND apply", but it only did the asking.
> When two operations are semantically related ("discard = revert"), make
> the code reflect that by calling through, not duplicating logic.

### 7.12 Tabs visible at startup (when no skill selected)

**Request.** At launch, neither Description nor Editor should appear active —
nothing's selected.

**Cause.** `QTabWidget` always has exactly one tab selected; there's no
"no selection" state.

**Fix.** Hide the tab bar via `tabs.tabBar().hide()` until a skill is selected,
then show on first selection. `clear()` re-hides on refresh.

> **Insight.** `QTabWidget` is conceptually a stacked widget plus a tab bar —
> the selection IS the bar. So "no tab selected" can only mean "no tab bar."
> Hide the bar, not the whole widget — that keeps the right pane's allocated
> space, so the splitter columns don't jump when the first skill is selected.

### 7.13 Type-back-to-original false positive (final dirty refactor)

**Symptom.** Modify a file. Type the changes back to the original. The dirty
marker stayed on; switching prompted "Unsaved changes" even though the buffer
matched disk byte-for-byte.

**Cause.** `modificationChanged` is a **flag-based** signal. Qt flips it to
True on the first edit and only flips it back when explicitly told. Manually
typing the original characters back doesn't undo the flag.

**Fix.** Replaced flag-based dirty with **content-based** dirty:
- Store `_pristine_text` snapshot on open / save / revert.
- On `textChanged`, compare `editor.toPlainText() != self._pristine_text`.
- Updated `_set_dirty` only when the answer changes (avoids redundant calls).

The previously-avoided `textChanged` noise from `QSyntaxHighlighter` is no
longer a concern — format changes don't alter `toPlainText()`, so the
content compare returns equal and the flag stays correct.

**Difficulty assessment** (made before implementing): 1/5. Single file,
~15 lines, no cross-cutting concerns, performance trivially fine for
typical SKILL.md sizes (a few KB → microseconds per keystroke).

> **Insight.** Two principles surface here that go beyond this one bug:
>
> 1. **"Dirty" is a UI concept, not a data-model concept.** The user's
>    mental model is "did I change the file?" — content equivalence, not
>    edit history. Flag-based dirty captures the WRONG question (did edits
>    occur?) when the user is asking the RIGHT one (does the buffer match
>    disk?). When UX and data-model semantics drift, follow the user.
>
> 2. **Local state pays off when requirements grow.** The earlier
>    `modificationChanged` choice wasn't wrong for the requirement at the
>    time. Requirements grew. The right answer changed. Because the dirty
>    state lived entirely in `EditorPanel`, the swap was a single-file
>    change. If it had been wired through `MainWindow` or `SkillListPanel`,
>    the fix would have been 5x bigger.

### 7.14 Skill enable/disable: collapsing a 4-state model to a binary toggle

**Background.** Claude Code controls skill visibility via `skillOverrides` in
`settings.json` / `settings.local.json` (and `enabledPlugins` for plugin
skills). The override is **4-state**: `on` / `name-only` / `user-invocable-only`
/ `off`. Absence equals `on`.

**Decisions.**

1. **UI shows a binary Enabled / Disabled toggle.** The two middle states
   (`name-only`, `user-invocable-only`) carry meaningful nuance — collapsing
   a `name-only` skill to `on` or `off` would silently drop it. So when a
   skill is at one of the middle states, the row is rendered with a
   bracketed `[name-only]` / `[user-only]` pill and the toggle is
   **disabled with a tooltip** pointing the user at `settings.local.json`.
   Power-user states stay readable; only binary toggles are writable.
2. **"Enable" removes the entry; only "Disable" writes a value.** A skill
   absent from `skillOverrides` is treated as `on`. Writing `"on"`
   explicitly is functionally redundant and pollutes the diff. Removing the
   entry mirrors what the `/skills` menu produces and keeps
   `settings.local.json` minimal.
3. **Plugin skills are read-only.** Per the docs, `skillOverrides` doesn't
   apply to plugin skills — they inherit `enabledPlugins[<plugin>@<m>]`.
   Toggling one plugin skill would semantically mean "disable the whole
   plugin," which is a different blast radius. The UI shows the inherited
   state (`[plugin off]` pill + dimming) but the toggle is disabled. A
   future iteration could add a plugin-level disable button if useful.
4. **Refusing to write into malformed JSON.** `write_override` reads, then
   modifies, then writes. If the existing JSON parses but isn't an object,
   or doesn't parse at all, the write raises and the user sees a dialog —
   we never silently overwrite. This protects unrelated keys
   (`permissions`, `env`, `hooks`) from being lost to a stray comma.
5. **State lives on `Skill`, computed once by the scanner.** The state is
   data the scanner already has the context to compute (it knows source,
   path, and plugin parent). Putting it on the dataclass means panels just
   read `skill.state`; they never re-read settings files for display.
6. **In-place row refresh after toggle, not a full rescan.** Toggling one
   skill calls `SkillListPanel.refresh_state(skill)`, which finds the
   matching `QTreeWidgetItem` and re-applies styling. The user's selection,
   scroll position, and group expansion all survive the toggle.
7. **Toolbar State filter uses the same binary collapse.** The toolbar's
   Enabled / Disabled checkboxes classify via `_state_group(state)` —
   `off` and `plugin-off` go to the Disabled bucket, everything else
   (including `name-only` and `user-invocable-only`) goes to Enabled.
   This keeps the user-facing mental model consistent across surfaces:
   the toggle button and the filter both speak in two states. The
   middle states still render distinctly (their `[name-only]` /
   `[user-only]` pill is preserved) — they just aren't a separate
   *filter* category.

> **Insight.** The "lose nuance vs. force advanced editing" tension here is
> the same one config UIs face all the time — Git's GUI clients hide most
> options behind defaults rather than exposing every flag. The right move
> is usually to make the common case effortless and the advanced case
> *visible but explicit*. Greying out a toggle with a clear tooltip is more
> honest than silently letting users round-trip a state through `on`.

### 7.15 Project skill overrides: write scope vs. read scope diverged for monorepo roots

**Symptom.** Disabling a Project skill greyed it out as expected. After
clicking Refresh the skill flipped back to Enabled. The toggle's on-disk
effect was correct (the override DID persist), but the scanner couldn't
see it on re-read.

**Cause.** Asymmetric scope derivation between the write and read paths.

The write side correctly derived per-skill scope:

```python
return skill.path.parents[1]   # the .claude/ above the skills/ directory
```

For a skill at `C:\projects\pyCOMPS_zdu\.claude\skills\hello\`, this
targeted `C:\projects\pyCOMPS_zdu\.claude\settings.local.json` —
correct.

The read side hardcoded the top-level project root:

```python
project_overrides = read_overrides(project_root.expanduser().resolve() / ".claude")
```

When `project_root` was `C:\projects` (a parent containing multiple
project subdirectories — a monorepo-shaped root, which the recursive
`_find_project_skills_dirs` walk explicitly supports), the read targeted
`C:\projects\.claude\`, which doesn't exist. Every project skill read
defaulted to `"on"`, silently dropping every override on Refresh.

**Why it didn't surface immediately.** With `project_root` set to a
single project (like `C:\projects\pyCOMPS_zdu`), `parents[1]` for every
skill *equals* `project_root / ".claude"` — read and write paths
coincide. The bug only appears the moment the user picks a
parent-of-many-projects root.

**Fix.** `_populate_states` now derives each skill's scope the same way
the write side does (`skill.path.parents[1]`), with reads cached per
scope so each settings file is hit at most once per scan. The
`project_root` parameter to `_populate_states` was unused after the fix
and was removed.

> **Insight.** When two code paths in a feature both touch the same
> resource, derive the location through the same expression. Otherwise
> they drift the moment recursion or polymorphism enters the picture.
> Same lesson as "have a single `get_user_id()` function" instead of
> recomputing it from the request in three places. The clean refactor is
> a one-line helper used by both sides — here, `skill.path.parents[1]`
> appears in `_scope_dir_for` and in `_populate_states.overrides_for`,
> and that intentional symmetry is what makes the feature work end-to-end.

### 7.16 Choose-root: state mutated before validation could abort

**Symptom.** After picking a new project root via the **Choose…** button,
the toolbar label updated to the new path but the skill list kept showing
the old root's skills. Reproducible only when the user had unsaved edits
in the editor at the moment they clicked Choose…, then clicked **Cancel**
on the resulting "Discard unsaved changes?" prompt.

**Cause.** Original ordering:

```python
if chosen:
    self._project_root = Path(chosen)   # ← mutate
    self._update_root_label()           # ← label shows new path
    self.refresh()                      # ← may abort inside confirm_close()
```

`refresh()` calls `editor_panel.confirm_close()` internally; cancelling
the discard prompt makes it return early without re-scanning. By that
point `_project_root` had already been written, and the toolbar label had
already been updated. The skill list's underlying `_all_skills` was
still the previous scan's output — visibly stale relative to the toolbar.

**Fix.** Confirm BEFORE mutating:

```python
if not chosen:
    return
if not self.editor_panel.confirm_close():
    return
self._project_root = Path(chosen)
self._update_root_label()
self.refresh()
```

The user is prompted once (here), and `refresh()`'s own
`confirm_close()` then returns True without re-prompting (Discard
already cleared the dirty flag).

> **Insight.** Two patterns could fix this. (1) Validate-before-mutate,
> as above. (2) Mutate, run, and roll back on failure. (1) is strictly
> better because it keeps the function's state machine simple — there's
> never a moment where toolbar and underlying state disagree, even
> briefly. (2) requires remembering the old value, which is exactly the
> kind of bookkeeping that rots when someone later adds a second piece
> of state to mutate alongside `_project_root`. The general rule: when
> a mutation is gated by a confirmation, do the confirmation first.
>
> A secondary lesson: programmatic tests with mocked `QFileDialog` did
> NOT catch this because they couldn't reproduce the "have unsaved
> edits at the moment of click" precondition. CLAUDE.md's "verify by
> launching the GUI" rule is doing real work here — interaction state
> from prior steps is exactly what synthetic tests miss.

### 7.17 Plugin discovery via marketplace manifest, not folder layout

**Symptom.** A user added the `idm-standards` marketplace via
`/plugin marketplace add …` and enabled all three of its plugins
(`idm-docs-plugin`, `idm-eng-plugin`, `idm-uplifter-plugin`). Claude Code
itself loaded their skills — `enabledPlugins` showed all three set to
`true` — but the GUI's Plugin group never listed them.

**Cause.** `scan_plugin` hardcoded a single layout:
`<m>/plugins/<plugin>/skills/<skill>`. Real marketplaces ship at least
three different layouts, all blessed by Claude Code:

| Layout | Example | Manifest source field |
|---|---|---|
| A: `<m>/plugins/<plugin>/skills/...` | `claude-plugins-official` | dict (`git-subdir`) — installs to `plugins/<plugin-name>` |
| B: `<m>/<source>/skills/...`         | `idm-standards`, `idm-agent-skills` | `"./<folder>"` — plugin is a direct child of the marketplace |
| C: `<m>/<source>/` + explicit `skills: [...]` | `anthropic-agent-skills` | `"./"` — multiple "logical plugins" share the marketplace root |

The walk found Layout A and missed B and C entirely. For the user's
real install that meant 17 plugin skills were silently invisible.

A *secondary* bug: `plugin_id` was derived from folder names
(`s.path.parents[1].name`). For `idm-standards` the folder is
`idm_docs_plugin/` (underscores) but the canonical plugin name is
`idm-docs-plugin` (hyphens, used in `enabledPlugins`). Even if the
walk had found these skills, every one would have shown
`plugin-off` because the id lookup against `enabledPlugins` would
have missed.

**Fix.** Read each marketplace's `.claude-plugin/marketplace.json` and
let it drive both layout AND naming:

- Top-level `name` → `<marketplace>` half of `plugin_id`.
- Each `plugins[]` entry's `name` → `<plugin>` half of `plugin_id`
  (this is what `enabledPlugins` keys on, regardless of folder name).
- `entry.source`: string `"./foo"` → `<m>/foo` (Layout B); dict or
  missing → `<m>/plugins/<plugin-name>` (Layout A).
- `entry.skills` list (Layout C) → resolve each path explicitly
  relative to the plugin source.

Plugin skills are stamped with `plugin_id` at scan time, so
`_populate_states` no longer derives anything from path components for
plugin skills — it only consults `enabledPlugins`.

Marketplaces without a manifest fall through to the legacy folder walk
(strictly more permissive than no fallback).

> **Insight.** The original walk inferred where things should be from
> the folder layout alone. That works only as long as every producer
> agrees on layout — which Claude Code's marketplace ecosystem
> deliberately does not. The manifest exists precisely to be
> authoritative, and Claude Code itself reads it. When two systems are
> looking at the same data, they should consult the same source of
> truth, or one of them will go stale the moment the other adds a new
> shape. Same lesson as §7.15 — when there's a manifest, prefer the
> manifest over re-deriving structure.
>
> A secondary lesson: silent fallouts ("only some plugins appear")
> are harder to diagnose than crashes. The path-walk returned an
> empty iterator for marketplaces that didn't match Layout A — no
> error, no warning, just missing data. If the scanner had logged
> "manifest mentions plugin X but no skills folder found" we'd have
> caught the layout drift on the first IDM install instead of waiting
> for a user to ask why their plugins weren't showing up.

### 7.18 Same-name plugin skills: on-collision disambiguation suffixes

**Symptom.** A user with two installs of the same marketplace
(`claude-plugins-official` + `claude-plugins-official.staging`) saw
several visually-identical rows in the left list:

```
skill-creator       (×2 — prod + staging)
claude-md-improver  (×2 — prod + staging)
configure           (×6 — discord/imessage/telegram × prod/staging)
```

Each row pointed to a real, distinct skill folder (the dedupe-by-path
in the scanner correctly kept them all), but nothing in the rendered
label distinguished them.

**Decision.** Add an *on-collision-only* disambiguation suffix. A
skill gets a `[<context>]` suffix only when its name appears 2+
times **within the same group header (Global / Plugin / Project) in
the currently visible list** — search/state filtering can remove a
suffix once a collision is filtered down to one row, and a name
appearing once in Global and once in Project earns no suffix at all
(the group headers already disambiguate visually).

**Why per-group rather than global.** The tree is grouped by skill
type with explicit headers above each section, so two same-named
skills under different headers are *already* visually distinct.
Suffixing them anyway adds noise to answer a question the layout
already answers. Buckets are keyed by `(skill.type, skill.name)`
rather than just `skill.name`, so the collision pass sees only the
peers that would actually render side-by-side under the same
header. This was tightened after the initial implementation when a
user noticed `explain-code` getting `[zhaoweidu]` / `[pyCOMPS_zdu]`
suffixes despite being unambiguous within Global and Project
respectively.

**Minimum-disambiguator algorithm.** For each name-collision group,
pick the shortest suffix that's actually informative:

| Variation among peers | Suffix |
|---|---|
| Same plugin, different marketplaces | `[<marketplace>]` |
| Same marketplace, different plugins | `[<plugin>]` |
| Both differ (or one peer has no plugin_id) | `[<plugin>@<marketplace>]` |
| Global / Project skills with name collision | `[<scope-folder>]` (parent of `.claude/`) |

**Marketplace value is the on-disk *folder* name, not the manifest's
canonical `name`.** This matters specifically for production-vs-staging
clones: both `claude-plugins-official/` and
`claude-plugins-official.staging/` ship a manifest with
`"name": "claude-plugins-official"`. The manifest name is the right
choice for `plugin_id` (because that's what `enabledPlugins` keys on,
so a one-time enable applies to both copies), but the wrong choice
for *display* — if we used it, the two staging twins would render
identically. Folder names, being sibling directories, are unique by
definition.

**Path-component lookup, not parents[N].** The marketplace folder sits
at different depths in the three plugin layouts (A: `parents[4]`, B:
`parents[3]`, C: `parents[2]`). `_marketplace_folder_for` walks
`Path.parts` looking for the literal `marketplaces` segment and
returns the next component — invariant under any future layout that
keeps the `marketplaces/<X>/...` naming convention.

**Composition with state suffixes.** The visual order of decorations
on a row is `<name>   [<disambig>]   [<state>]` so a single-line row
might read e.g. `configure   [discord@…]   [plugin off]`. The
disambiguation suffix is stored per item via a custom data role
(`_DISAMBIG_ROLE = Qt.UserRole + 1`) so `refresh_state(skill)` —
called when a user clicks Enable/Disable on a single row — can
preserve the suffix without re-running the collision pass over the
whole list.

> **Insight.** Two principles worth preserving from this fix:
>
> 1. **"Show context only when ambiguous" beats "show context always."**
>    The temptation is to slap `[plugin@marketplace]` on every plugin
>    skill — predictable, simple, and blocks the user's question
>    before they ask. But it costs constant screen real estate to
>    answer a question only ~5% of skills actually pose. On-collision
>    detection trades a tiny bit of code (one pass to count names) for
>    a much quieter UI in the common case.
>
> 2. **Identity for matching ≠ identity for display.** `plugin_id`
>    (manifest-derived) is the right key for `enabledPlugins` lookup
>    because that's what the user enabled. The marketplace *folder*
>    is the right token for distinguishing two physical installs in
>    the UI. When two systems use the same data for different purposes,
>    they often need slightly different keys — fighting that creates
>    bugs in whichever direction you collapse them.

### 7.19 PLUGIN_OFF: faded icon instead of "[plugin off]" suffix

**Request.** The `[plugin off]` text suffix on every plugin-disabled row
felt noisy, especially when combined with a disambiguation suffix
(`access   [discord@claude-plugins-official]   [plugin off]` is a lot
of text per row). Replace with a visual indicator.

**Constraint.** PLUGIN_OFF must remain *distinguishable from* binary
OFF (user-disabled), not just look "disabled" in general. The two
states require different actions to re-enable — OFF is reversible via
right-click → Enable, PLUGIN_OFF requires `/plugin`. Both are
currently styled italic + grey; if we drop the `[plugin off]` text
without adding another visual axis, the two collapse to identical.

**Decision.** Render the type icon at reduced opacity (`0.35`) for
PLUGIN_OFF rows. Same shape (still says "this is a Plugin skill"),
muted color reads as "inherited-disabled, manage elsewhere." The
parallel `_icon_for_faded` cache keeps the per-type painting code
single-source via a shared `_paint_type_icon(type, opacity)` helper —
no second copy of the shape geometry to drift out of sync.

| State | Icon | Font | Suffix |
|---|---|---|---|
| ON                  | full color  | regular | (none) |
| OFF                 | full color  | italic + grey | (none) |
| **PLUGIN_OFF**      | **faded (0.35)** | italic + grey | (none — was `[plugin off]`) |
| NAME_ONLY           | full color  | softer grey | `[name-only]` (kept — see §7.14) |
| USER_INVOCABLE_ONLY | full color  | softer grey | `[user-only]` (kept) |

**Tooltip carries the "why".** Removing the suffix loses the
"manage via /plugin" hint that used to be visible-by-default. We
recover it on hover by appending `\n(plugin disabled — manage via
/plugin)` to the row's tooltip when `state == STATE_PLUGIN_OFF`. The
information is still there, it just costs an intentional hover instead
of constant horizontal pixels.

**Icon ownership moved into `_apply_state_style`.** Previously the
icon was set by `_rebuild` and `_apply_state_style` only changed
font/color. Now the icon is part of the state visual, set in the same
function that paints font/color. Side benefit: `refresh_state(skill)`
— called when the user toggles Enable/Disable — gets correct icon
swapping for free. Even though PLUGIN_OFF isn't directly toggleable
today, this future-proofs the code for hypothetical "Disable plugin"
affordances (mentioned as a §9 Bonus).

> **Insight.** Two general principles surface here:
>
> 1. **Encode each state distinction on its own visual axis.**
>    Type was already encoded via icon shape+color (deliberately
>    distinct on both axes for color-blind accessibility — see the
>    comment on `_TYPE_PAINT`). Disabled-ness was encoded via font
>    style + color. PLUGIN_OFF needed a third axis distinct from
>    OFF — opacity of the icon was the obvious unused channel.
>    When you find yourself adding text to disambiguate two states
>    that *look* the same, ask first whether there's an unused
>    visual channel that could carry the distinction more quietly.
>
> 2. **A tooltip is the right home for a "why" that doesn't fit on
>    the row.** Per the existing tab-render pattern (§7.8), the
>    Description preview re-renders on tab switch rather than per
>    keystroke — same principle: surface things the user is
>    actually asking about, hide things they aren't. Constant
>    "[plugin off]" text answers a question users only ask
>    occasionally, by occupying horizontal space *every* render.

### 7.20 Broaden faded-icon rule to all "off" states

**Request.** Apply the faded type icon to OFF Global/Project skills as
well, not just PLUGIN_OFF Plugin skills.

**Why it works.** OFF and PLUGIN_OFF are mutually exclusive by skill
type — Global/Project skills are never PLUGIN_OFF (that's plugin-only)
and Plugin skills are never OFF (they aren't user-toggleable
individually, only via `/plugin`). So unifying the visual treatment
doesn't collapse two states the user could ever see on the same row;
the icon *shape* (circle/square/diamond → Global/Project/Plugin) by
construction tells you which kind of "off" applies. Opacity becomes
a uniform "off-ness" channel across all types.

**Effect on §7.19's table.** OFF moves from "full color icon, italic
+ grey" to "**faded** icon, italic + grey" — same as PLUGIN_OFF. The
two STATE branches in `_apply_state_style` collapse into a single
`if state in (STATE_OFF, STATE_PLUGIN_OFF):` branch.

**Tooltip differentiation preserved.** The PLUGIN_OFF-only tooltip
hint (`(plugin disabled — manage via /plugin)`) stays where it is
in `_rebuild`. OFF rows don't get an extra hint because the user
toggled the state themselves and already knows how to reverse it
(right-click → Enable). This keeps the noise asymmetric in the
right direction: surface the explanation only when the user might
not know it.

**Right-click menu unaffected.** Enable/Disable affordances are
gated on `BINARY_STATES` and `is_plugin` checks in
`_on_context_menu`, not on visual styling. A faded OFF row stays
user-toggleable; a faded PLUGIN_OFF row stays read-only. The icon
is presentation-only.

> **Insight.** The earlier (§7.19) version of this fix hesitated to
> fade OFF because OFF and PLUGIN_OFF would collapse to identical
> visuals. That worry only materializes if both states could
> co-occur on the same row, which they can't — the type system
> already partitions them. When you hesitate to apply a clean rule
> uniformly, check whether your hesitation reflects a real
> ambiguity or a phantom one. Phantom ambiguities create
> special-case code paths whose only purpose is to encode an
> ambiguity that doesn't exist; collapsing them simplifies both
> the implementation AND the user's mental model ("disabled looks
> faded, period").

### 7.21 App logo: programmatic three-shapes composite

**Request.** Add a logo to the main app frame (title bar / taskbar /
Alt+Tab).

**Decision.** Paint the logo programmatically rather than ship a PNG.
Composite the three skill-type shapes (indigo circle + green rounded
square + amber diamond) in a triangular arrangement, matching the
existing per-type palette in `skill_list._TYPE_PAINT`.

**Why programmatic.**

1. **Zero-asset.** The app already paints its skill-type badges via
   `QPainter`; the logo joins that conventions and keeps the repo
   image-free. No `.png`/`.ico`/`.svg` files to ship, version, or
   regenerate when the palette changes.
2. **Crisp at every size.** A multi-size `QIcon`
   (`addPixmap` for 16/32/48/64/128/256 px) lets Windows pick the
   pre-rendered size matching whatever surface needs it — title bar
   (16), taskbar (32 typical), Alt+Tab (48–64), Settings dialogs
   (128). No runtime downscaling artefacts at the smallest sizes.
3. **Identity tied to the app.** The logo *is* the three-source skill
   model the app exists to manage. Anyone seeing the icon next to a
   row of icons in the GUI immediately recognises that the row icons
   and the app icon are saying the same thing in the same language.

**Palette duplication, deliberately.** `app_icon.py` defines its own
`_GLOBAL_COLOR` / `_PROJECT_COLOR` / `_PLUGIN_COLOR` rather than
importing from `skill_list._TYPE_PAINT`. The two modules are leaf-level
peers in `ui/`; importing one from the other would create a private
internal coupling for two strings of meta-data. The trade is that a
future palette change requires updating both — explicit duplication
beats implicit coupling for two leaves with no shared base.

**Wiring.** `main.py` calls `app.setWindowIcon(app_icon())` after
`QApplication` construction (required — `QPixmap` needs the GUI
subsystem). Setting on `QApplication` propagates to any window without
its own icon, so `MainWindow` doesn't need a separate call.

**Geometry as fractions.** `_paint_physical(size)` expresses every
coordinate as a fraction of the canvas (`0.50 * size`, `0.18 * size`),
so the layout scales linearly across sizes without per-size special
casing. Integer-rounding before each draw call keeps small renderings
(16 px) sharp on pixel boundaries.

**Toolbar placement** (subsequent iteration). The same logo also
appears at the leftmost position of the main toolbar as a visual
brand anchor. This required a small refactor: `_paint_physical(size)`
became the shared core, with two thin public layers on top —
`app_icon()` for window-icon use (multi-size pack, no DPR), and
`app_logo_pixmap(logical_size)` for in-window use (single pixmap
painted at 2× and tagged `setDevicePixelRatio(2.0)` for HiDPI
sharpness, matching the convention in `skill_list._paint_type_icon`).

The toolbar logo uses 24 logical px — Qt's default toolbar icon
footprint, fits inside `BUTTON_STYLE`'s `min-height: 22px` cap so
the toolbar height stays the same. It sits before the "Project root:"
label with 6 px left / 8 px right padding and **no `addSeparator()`**
after it: the padding alone is enough visual break, and a separator
adjacent to the title-bar boundary would crowd the leftmost edge.

> **Insight.** Whenever the same painted asset needs both a
> multi-size icon form and a single-pixmap form, factoring out the
> physical-pixel renderer is what lets both consumers share *exactly*
> the geometry. The two public functions become trivial thin
> wrappers — `app_icon` packs a list of sizes, `app_logo_pixmap`
> attaches a DPR — and a future palette tweak only touches one place.

> **Insight.** Two takeaways:
>
> 1. **Programmatic > assets, when the design is shape-based.** Any
>    icon you'd describe as "three coloured shapes in a layout"
>    should be paintable in 50–80 lines of Qt and beats a `.png`
>    bundle on every axis: smaller repo, no DPI variants to manage,
>    no resampling at small sizes. The threshold where assets win
>    is "design has photographic detail or hand-drawn artwork."
>    For a flat palette + geometric shapes, paint it.
>
> 2. **Coupling between leaf modules costs more than it saves.**
>    `app_icon.py` and `skill_list.py` use the same three colour
>    constants but do not share them in code. The "DRY" instinct
>    would centralise them in a `palette.py` constant module — but
>    that creates a third module and a fan-in dependency that
>    earns nothing concrete. Two strings in two files is fine when
>    the modules don't otherwise need to know about each other.
>    DRY is a tool, not a law; the cost of an abstraction has to
>    pay for itself.

### 7.22 Windows taskbar identity: AppUserModelID

**Symptom.** The Windows taskbar showed the Python interpreter's icon
(or a generic Windows app icon) for the running app, even though
`QApplication.setWindowIcon(app_icon())` was set and the title bar
showed the custom logo correctly.

**Cause.** Windows identifies running apps in the taskbar by an
**AppUserModelID** — a process-level identity string. When you launch
a Python script, the OS uses `python.exe`'s AppUserModelID by default,
so the taskbar uses *Python's* icon (not Qt's `setWindowIcon`).
Title bar, Alt+Tab, and any in-window pixmap surface (toolbar, etc.)
work fine because those paths read the QIcon directly — they don't go
through the AppUserModelID system. Only the taskbar entry does.

Installed apps (PDF readers, PowerPoint, etc.) avoid this because their
installer registers a unique AppUserModelID per-app, either via the
shortcut's `System.AppUserModel.ID` property or in the executable's
manifest. Run-from-source Python apps inherit `python.exe`'s identity
unless they take the same step at runtime.

**Fix.** A single Win32 call before `QApplication` construction:

```python
import ctypes
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
    "ClaudeSkillsManager.ClaudeSkillsManager")
```

Wrapped in `_set_windows_taskbar_identity()` in `main.py` with:
- A `sys.platform == "win32"` gate (no-op on macOS / Linux).
- `try / except (AttributeError, OSError)` — degrades to "taskbar
  shows python.exe's icon, in-window surfaces still work" on
  older Windows or non-default shells.

**ID format.** `Company.Product[.SubProduct[.Version]]` per Microsoft's
guidance. We use `ClaudeSkillsManager.ClaudeSkillsManager`, matching
the QSettings organization/app pair so all per-user identity for this
app keys off the same string. Multiple instances of the app share the
ID, so they group together in the taskbar (same as a regular Windows
app).

**Ordering.** `SetCurrentProcessExplicitAppUserModelID` MUST run before
the first window is shown. Calling it before `QApplication`
construction in `main()` is the simplest correct ordering — there's
no Qt dependency in the call itself, just `ctypes.windll`.

**What this does NOT change.**
- The icon shown for `.py` files in Explorer (controlled by file
  association, not AppUserModelID).
- The icon for a *frozen* build (PyInstaller / py2exe) — those bake
  an icon into the .exe resource section. AppUserModelID is the
  run-from-source equivalent.
- Already-running instances launched before the fix took effect —
  those are bound to python.exe's ID and won't update until they
  restart.

**Follow-up: per-window icon required for taskbar binding.** After
applying just the AppUserModelID fix, the taskbar still showed the
generic Python icon. Diagnosis confirmed
`SetCurrentProcessExplicitAppUserModelID` was succeeding
(`GetCurrentProcessExplicitAppUserModelID` read back the same string),
so the call wasn't the issue. The actual missing piece:
`QApplication.setWindowIcon` and `QWidget.setWindowIcon` go through
*different Win32 paths*. The application-level call only sets a
fallback default that child windows inherit if they don't override
it; the **per-window** call is what triggers a `WM_SETICON` message
to the OS, which is what the Windows taskbar binds to.

So the complete fix is two-layer:
1. **`main.py`** → `app.setWindowIcon(app_icon())` — covers child
   windows like `QMessageBox` that don't set their own.
2. **`MainWindow.__init__`** → `self.setWindowIcon(app_icon())` —
   covers the taskbar entry for the main window itself.

Both calls are needed; neither is redundant. Installed Windows apps
(PDF readers, PowerPoint) effectively do the same thing — the icon
is registered against both the executable identity AND each
top-level window.

**Icon cache caveats.** Windows caches taskbar icons aggressively by
AppUserModelID. If the belt-and-suspenders fix doesn't visibly take
effect on first launch, the cache is the most likely culprit:

- Close ALL existing instances of the app first (already-running
  instances stay bound to whatever icon they started with).
- If still wrong, restart Explorer (`taskkill /f /im explorer.exe`
  then `start explorer.exe`) — refreshes the taskbar's icon view
  without touching the cache file.
- Last resort: delete `%localappdata%\IconCache.db` and the
  `%localappdata%\Microsoft\Windows\Explorer\iconcache_*.db`
  family, then restart Explorer.

**Final fix: per-window IPropertyStore + on-disk .ico.** Even with
the AppUserModelID call AND `QMainWindow.setWindowIcon`, the taskbar
still showed a blank/generic icon. A runtime diagnostic confirmed
`WM_GETICON` returned non-zero handles for `ICON_SMALL`/`ICON_BIG`/
`ICON_SMALL2` — Qt *was* attaching the icon to the window. So
`WM_SETICON` itself wasn't the bottleneck.

The actual root cause: **the Windows taskbar resolves icons by
AppUserModelID FIRST, falling back to the per-window
`WM_SETICON` only as a secondary surface (title bar, Alt+Tab).**
For installed apps, the AppUserModelID is registered against an .exe
or .ico resource by the installer, so the taskbar always finds an
icon. For run-from-source apps that set the AppUserModelID at
runtime, no icon is registered, and the taskbar shows blank — even
though every other in-app surface works.

The fix Microsoft documents for runtime-set AppUserModelIDs is to
attach an icon resource to the window's IPropertyStore via
`SHGetPropertyStoreForWindow`, setting two property keys:

| Property | Purpose |
|---|---|
| `PKEY_AppUserModel_ID`               | Per-window AppUserModelID (overrides the process-wide one for this specific window). |
| `PKEY_AppUserModel_RelaunchIconResource` | `<.ico path>,<index>` — tells the shell where to read the icon for this window's taskbar entry. |

We do this in three pieces:

1. **`app_icon.write_logo_ico(path)`** — paints the composite at
   256 px and saves it to disk via `QPixmap.save(path, "ICO")`.
   Qt's bundled image plugins handle ICO writing — no PIL/Pillow
   dependency.
2. **`ui/win32_taskbar.py`** — pure-ctypes COM caller. Defines the
   GUID/PROPERTYKEY/PROPVARIANT structs, calls
   `SHGetPropertyStoreForWindow`, walks the `IPropertyStore`
   vtable to invoke `SetValue` twice and `Commit`, then
   `Release`s the interface. Wraps every failure path in a
   silent return-False so cosmetic taskbar issues never crash the
   app on launch.
3. **`MainWindow.showEvent`** (one-shot via `_taskbar_icon_bound`
   guard) — writes the .ico to `%TEMP%\ClaudeSkillsManager_logo.ico`
   on first show, then calls `apply_window_appusermodel(self.winId(),
   APP_ID, ico_path)`. The HWND must exist before
   `SHGetPropertyStoreForWindow` is called, so `__init__` is too
   early — `showEvent` is the right hook.

**Why ctypes instead of comtypes/pywin32.** The existing dependency
list (DESIGN.md §1) is intentionally minimal: only PySide6 and
PyYAML. ctypes is in the standard library and was already used for
`SetCurrentProcessExplicitAppUserModelID`. Adding comtypes for one
COM call would be a much heavier dependency than the ~80 lines of
ctypes plumbing it replaces. The COM dance is well-documented and
the vtable indices for `IPropertyStore` are stable Win32 ABI.

**`.ico` file lifecycle.** Written to `%TEMP%` once per session, kept
on disk for the lifetime of the running app (Windows reads it lazily
on each taskbar repaint). The OS cleans up `%TEMP%` files
periodically, which is fine — we recreate on next launch if missing.

> **Insight.** Three takeaways from this debugging chain:
>
> 1. **"It's set but nothing changed" usually means a different
>    code path reads the value.** Our `WM_SETICON` was set; the
>    title bar showed it; the taskbar didn't. That asymmetry was
>    the clue: surfaces that *did* work read the icon directly
>    from the window, surfaces that *didn't* work read it from
>    the AppUserModelID's registered resource. Identifying the
>    asymmetric surface narrowed the diagnosis from "Qt is
>    broken" to "Windows taskbar reads from a different place."
> 2. **Diagnostic instrumentation pays off when the symptom is
>    cosmetic.** A 100-line throwaway script that printed `WM_GETICON`
>    return values and AppUserModelID read-back was the difference
>    between "guess and check" and "definitive answer." For runtime
>    behavior questions, build the diagnostic; don't reason in the
>    dark.
> 3. **Run-from-source has different shell-integration semantics
>    than installed apps.** PDF readers and PowerPoint don't have
>    this problem because their installer wrote registry entries.
>    Run-from-source apps need to do the equivalent at runtime —
>    via `IPropertyStore`. This is why so many Python GUI apps
>    ship with the wrong taskbar icon: the canonical Stack Overflow
>    answer (just call `SetCurrentProcessExplicitAppUserModelID`)
>    is incomplete for the taskbar surface.

> **Insight.** Two patterns worth preserving:
>
> 1. **Platform identity is a separate concern from in-app
>    presentation.** Qt's `setWindowIcon` is correct for everything
>    Qt itself manages — title bar, Alt+Tab, toolbar inline — but
>    the taskbar item is owned by the OS shell, which has its own
>    identity model (AppUserModelID on Windows, `CFBundleIdentifier`
>    on macOS, `.desktop` `StartupWMClass` on Linux). Use Qt's API
>    for in-app surfaces, OS APIs for shell surfaces.
>
> 2. **Fail-soft on platform calls.** The Win32 call could fail in
>    a sandboxed Windows environment or be missing on very old
>    versions. Catching `AttributeError`/`OSError` and skipping
>    means the in-app icon still works — only the taskbar entry
>    falls back. Better than crashing on launch over a cosmetic
>    issue.

### 7.23 Search-box magnifier (and the latent DPR bug it uncovered)

**Request.** Add a leading magnifier icon to the toolbar's search
``QLineEdit``, like a typical web search box.

**Implementation.** New ``ui/_icons.py`` module (a sibling to
``app_icon.py`` — the latter is *brand* identity, the former is
general-purpose UI icons). Single function ``search_icon()`` paints a
stroked grey magnifying glass (lens circle + diagonal handle) at
32 px physical with DPR=2.0 metadata for HiDPI sharpness. The icon is
attached via ``QLineEdit.addAction(action, LeadingPosition)`` — Qt
reserves space inside the input, shifts the placeholder right, and
handles HiDPI scaling automatically. The action is intentionally
unconnected: the search is already live-as-you-type via
``textChanged``, so the leading icon is purely a marker (per
common-web-search convention; trailing-position icons are typically
the action button).

**Latent bug uncovered while testing: `setDevicePixelRatio` ordering.**

The first version of ``search_icon()`` rendered as only the upper-left
arc of the lens circle, missing the rest of the lens and the entire
handle. A simple diagnostic — paint identical shapes at DPR=1 vs
DPR=2 and save both — confirmed the cause:

> **`QPainter` on a pixmap with DPR=2.0 set BEFORE painting interprets
> draw coords as logical, not physical.** A 32×32 physical pixmap with
> DPR=2 becomes a 16×16 *logical* canvas to the painter. Drawing at
> physical-style coords like `(4, 4)–(28, 28)` then clips because most
> of that range is outside the 16×16 logical area.

This wasn't a new bug — it was a *pre-existing* bug in
``skill_list._paint_type_icon`` that had been hiding in plain sight.
Every Global circle in the tree was a quarter-circle. Every Project
square was a quarter-rounded-square. Every Plugin diamond was a
triangle. The icons looked "designed" because they were small and the
clipped shapes had a kind of internal consistency, but they were
quietly wrong. A live grab of the skill list (``window.skill_list.grab()``)
into a PNG made the clipping unmistakable.

**Fix (applied to both `_paint_type_icon` and `search_icon`).** Set
``setDevicePixelRatio(2.0)`` *after* painting, not before:

```python
pix = QPixmap(32, 32)               # plain pixmap, DPR=1
pix.fill(Qt.transparent)
painter = QPainter(pix)
# … paint at physical coords (0–32 range) …
painter.end()
pix.setDevicePixelRatio(2.0)        # tag AFTER painting
```

When DPR is 1.0 during painting, the painter operates in physical
coords. The shape lands fully inside the 32×32 buffer. Tagging DPR=2.0
afterward is purely metadata for Qt's icon-resolution code to know
the pixmap represents a 16×16 *logical* icon — the painted pixels are
already correct.

**Why `app_icon.py` was unaffected.** ``_paint_physical(size)`` doesn't
set DPR at all (the public ``app_logo_pixmap`` sets it after calling the
painter). That's why the brand logo has always rendered correctly while
the type icons were silently clipped. Same painting framework, two
different orderings, two very different outcomes.

> **Insight.** Three takeaways:
>
> 1. **"Looks deliberate" is not the same as "is correct."** Three
>    distinct shapes that all happened to fail in similar
>    geometric ways looked like a coherent design choice. We
>    only noticed when a fresh icon (the search magnifier) used
>    the same code pattern and rendered visibly broken in a new
>    context. Latent visual bugs can hide for entire iterations
>    of a UI if every consumer of the broken code path tolerates
>    the failure mode the same way. The fix had been one line
>    away the whole time.
>
> 2. **Use a small isolating diagnostic.** "Paint a circle at
>    DPR=1 vs DPR=2 and save both" was 15 lines of code that
>    settled a question the live UI obscured for months.
>    Reasoning about Qt internals from the docs led to circular
>    arguments; saving two PNGs side-by-side gave a definitive
>    answer in seconds. Same pattern as the §7.22 diagnostic that
>    settled the taskbar-icon question — when something visual
>    is wrong, instrument the rendering.
>
> 3. **Order of side effects matters more than reads-equal-writes.**
>    ``setDevicePixelRatio`` is technically a pure setter — it
>    doesn't *do* anything to existing pixels. But it changes the
>    interpretation of *future* operations on the pixmap, which is
>    why call ordering matters. Whenever a setter changes the
>    semantics of subsequent calls, prefer to apply it AFTER the
>    work that depends on the old semantics — same lesson as
>    "validate before mutate" in §7.16, just for state-machine
>    semantics rather than user state.

---

### 7.24 Conditional Preview tab for `.md` files

**Request.** When the user clicks a ``.md`` file in the middle file
tree, the right panel should expose a **Preview** tab next to the
**Editor** tab that renders the file's markdown — like the existing
Description tab does for SKILL.md, but for whatever ``.md`` file is
currently open. The tab should only appear when the open file is a
``.md``. As part of this change, rename the existing **Description**
tab to **Skill Description** to disambiguate the two markdown views.

**Why two markdown tabs aren't redundant.** Description and Preview
look superficially similar (both render markdown via
``QTextBrowser.setMarkdown``) but track different things:

| Tab | Source | When visible | Header synthesis |
|---|---|---|---|
| Skill Description | The *selected skill's* SKILL.md | Always (once a skill is selected) | Yes — name + description + token count |
| Preview | The *currently-open file* (live editor buffer) | Only when ``open_file().suffix == ".md"`` | No — body only |

The two diverge as soon as the user clicks any ``.md`` file other
than SKILL.md inside a skill (e.g. a referenced doc, a sub-page).
Description still shows the skill's identity card; Preview shows
the file under edit. Combining them would lose one of the two views.

**Implementation.** Three pieces in
``ui/editor_panel.py``:

1. **Stable tab indices as class constants** —
   ``_DESCRIPTION_TAB_INDEX = 0``, ``_EDITOR_TAB_INDEX = 1``,
   ``_PREVIEW_TAB_INDEX = 2``. ``setCurrentIndex`` and
   ``setTabVisible`` calls reference these instead of magic numbers,
   so a future tab insertion is a single-line change.
2. **Visibility-toggling, not add/remove** —
   ``QTabBar.setTabVisible(index, bool)``. Removing/re-adding tabs
   would invalidate indices and break any stored references; toggling
   visibility preserves them. The Preview tab is constructed once at
   ``__init__`` time and hidden until ``open_file`` sees a ``.md``
   suffix.
3. **Lazy render on tab change** — same pattern Description already
   uses (``_on_tab_changed``). When the user switches *to* Preview,
   ``_render_md_preview`` reads ``editor.toPlainText()`` (live buffer,
   so unsaved edits show), strips a leading YAML frontmatter block,
   and calls ``setMarkdown``. Cost is paid only when the tab is
   actually viewed; typing into a ``.md`` file doesn't re-render
   Preview on every keystroke.

**Frontmatter handling.** Preview reuses the existing
``_strip_frontmatter`` helper. SKILL.md files have YAML frontmatter
that would otherwise dump as raw ``--- name: foo …`` text in the
rendered view. The function is unchanged from §6 — just gets a
second caller.

**Edge cases handled by the existing structure.**

* **`clear()`.** Hides both tab bar and Preview tab, blanks both
  ``QTextBrowser``s. State is fully reset.
* **Switching from a `.md` to a `.py`.** ``setTabVisible(False)``
  hides Preview. If Preview was the active tab, Qt auto-switches to
  the previous visible tab; ``open_file`` then explicitly
  ``setCurrentIndex(EDITOR)``, landing the user where they expect
  after opening a file.
* **SKILL.md as the open file.** Preview is visible *and* Description
  applies. Both tabs render the same underlying file but with
  different framing — Description adds the synthesized header,
  Preview just renders the body. Intentional duplication: the user
  can pick whichever framing they want without the app having to
  guess.

> **Insight.** **Two near-identical components are sometimes correct,
> not redundant.** Skill Description and Preview both use
> ``QTextBrowser.setMarkdown`` and both strip frontmatter — the
> temptation to "factor out the duplication" would couple two views
> with different lifecycles (skill-scoped vs file-scoped) and
> different framing (header-synthesizing vs raw). Keeping them as
> two small, parallel paths costs ~15 lines and removes a future
> refactor's worth of coupling. The right unit of reuse here is the
> *helper function* (``_strip_frontmatter``), not the tab itself.

---

### 7.25 Restore tree selection when "Unsaved changes" is cancelled

**Bug.** With unsaved edits to ``a.md``, clicking ``b.md`` in the file
tree opens the "Discard unsaved changes?" dialog. Clicking **Cancel**
correctly leaves the editor on ``a.md`` — but the tree highlight has
already moved to ``b.md``, so the file-tree selection and the editor
content are out of sync. The user clicks Cancel intending "I want to
stay on a.md," then sees ``b.md`` selected.

**Root cause: view-state racing the model.** ``QTreeView.clicked``
fires *after* the visual selection has moved to the clicked row.
By the time the chain reaches ``EditorPanel.open_file`` and the
``Cancel`` decision returns False, the tree's own selection model
has already updated. There's no "veto the click" hook in Qt's tree
— programmatic restoration is the only path.

**Fix.** Three small additions wired through the existing
signals-up / methods-down architecture (§8.3):

1. ``EditorPanel.open_file`` now returns ``bool`` — ``True`` on
   accepted open, ``False`` on cancel / non-file / read failure.
2. ``EditorPanel.current_path()`` exposes ``self._current_path`` so
   callers don't have to reach into private state.
3. ``FileTreePanel.select_path(path)`` programmatically moves the
   current row + scrolls into view. ``QTreeView.clicked`` is
   mouse-only — programmatic selection changes don't fire it — so
   this is recursion-safe; no risk of looping back into
   ``on_file_activated``.

``MainWindow.on_file_activated`` now restores tree selection on a
``False`` return:

```python
if not self.editor_panel.open_file(path):
    current = self.editor_panel.current_path()
    if current is not None:
        self.file_tree.select_path(current)
```

**Why not "veto before the visual change"?** ``QTreeView`` doesn't
expose a pre-click hook. Subclassing ``mousePressEvent`` and
intercepting before the selection model fires would technically
work, but would require ``FileTreePanel`` to know about editor
dirtiness, breaking the §8.3 layering ("signals up, methods down,
no cross-panel reach-ins"). The post-hoc restore pattern keeps the
editor as the only owner of dirty-state policy, the file tree as a
dumb selector, and ``MainWindow`` as the conductor that closes the
loop.

> **Insight.** **A "method that returns success" is a small
> protocol** — the caller can choose whether to react. ``open_file``
> used to be a void method on the assumption that the editor's
> internal state was self-consistent. That was true *for the editor*
> but not for *its callers*: the file tree had its own state to
> reconcile. Returning ``bool`` is the minimum disclosure that lets
> the caller stay in sync without exposing the dirty-state machinery.
> Same pattern as ``confirm_close()`` further up — a handshake
> instead of a fire-and-forget.

---

### 7.26 State-driven tab visibility (no skill / skill / file / .md file)

**Bug.** Although the tab bar was supposed to be hidden in the
"no skill selected" state (app start, after Refresh, after clearing
the search box), it leaked the Editor tab through in some cases:

* **Switching skills** — ``show_skill`` showed the tab bar and selected
  the Description tab, but never hid Editor. Editor had been built
  without any ``setTabVisible(False)`` ever applied to it, so it sat
  visible alongside Description even though no file was open in the
  new skill yet.
* **Clearing the search box** — the search's X (``setClearButtonEnabled``)
  fired ``textChanged("")`` which only updated the filter; the editor
  panel was never told to reset, so it kept showing whatever skill +
  file the user had been on before, including the Editor tab.

**Spec (per user).**

| State                                  | Tabs visible                          |
|----------------------------------------|---------------------------------------|
| No skill selected                      | *(tab bar hidden entirely)*           |
| Skill selected, no file open           | Skill Description                     |
| Skill selected, non-``.md`` file open  | Skill Description + Editor            |
| Skill selected, ``.md`` file open      | Skill Description + Editor + Preview  |

The "no skill" state covers app start, Refresh, **and** clearing the
search box — the parenthetical in the spec made this explicit: those
three gestures all mean "there's no skill selected yet."

**Fix.** Three coordinated changes:

1. **``_apply_tab_visibility`` as single source of truth** in
   ``EditorPanel``. The method reads ``(self._current_skill,
   self._current_path)`` and computes every tab's visibility plus
   the tab bar's own ``setVisible``. Every state-changing entry point
   (``show_skill``, ``clear``, ``open_file``) ends with a call to it
   instead of flipping individual tabs. The previous design scattered
   ``setTabVisible`` calls across three methods, which is what let
   the Editor-leaks-through bug exist in the first place — there was
   no method whose job was to say "given the current state, here is
   the entire visibility configuration."

2. **``_reset_file_state`` helper** shared between ``clear()`` (full
   reset) and ``show_skill()`` (skill-switch). Resetting file state on
   skill switch is the subtle half of the fix: when the user clicks
   skill B while a file from skill A is open in the Editor, that file
   is no longer in B's file tree, so leaving the Editor tab open
   showing it would be confusing. The Editor tab now reappears only
   when the user clicks a file in the new skill's tree.

3. **Search-clear resets selection.**
   ``main_window._on_search_changed`` intercepts ``textChanged`` and,
   when the text goes empty and a skill is currently selected,
   resets all four downstream panels and the tree's visual
   highlight via ``skill_list.clear_selection()``. The empty-search
   gesture is treated as "start over" — same end state as Refresh
   without the rescan.

**``clear_selection`` blocks signals.** ``setCurrentItem(None)`` and
``clearSelection`` would normally fire ``itemSelectionChanged`` → the
``_on_selection`` slot. Even though that slot early-returns on empty
selection, the programmatic-reset path shouldn't re-enter the
selection machinery at all — ``blockSignals`` is the safer pattern.

**Why ``setTabVisible`` and not ``addTab``/``removeTab``.** The user
spec says tabs "added" / "displayed," but removing-and-re-adding
tabs would invalidate the ``_DESCRIPTION_TAB_INDEX`` / ``_EDITOR_TAB_INDEX``
/ ``_PREVIEW_TAB_INDEX`` class constants every time. Toggling
visibility on tabs that always exist (just sometimes hidden) gives
the same visual result while keeping the indices stable — which is
what ``setCurrentIndex`` and the conditional-render logic in
``_on_tab_changed`` both depend on. See §7.24's "visibility-toggling,
not add/remove" note for the precedent.

> **Insight.** **A "compute the whole configuration from state"
> method is cheaper than scattered partial mutations.** The pre-fix
> design had each entry point flip the tabs it cared about and leave
> the rest alone — which meant the burden of correctness was "every
> entry point must remember every tab." Centralizing into one method
> that re-derives every tab's visibility from
> ``(_current_skill, _current_path)`` collapses that burden to "make
> sure the state variables are accurate." It's the same pattern as
> idempotent reducers in UI frameworks (state → render is a pure
> function), and it generalizes well to other "which thing is
> visible right now?" questions across the codebase.

---

### 7.27 Drop selection when the user starts typing in the search box

**Spec.** Continuing the "search box = context change" thread from
§7.26: when the user starts typing into the search box, the middle
(file tree) and right (editor) panels should go blank — "since there
is no skill is selected."

**Why this isn't trivial.** A naïve "reset on every non-empty
``textChanged``" would prompt the user about unsaved changes on
*every keystroke* — type "h", prompt; type "e", prompt; etc. That's
unusable. The reset has to fire **once** per "user changed context,"
not once per character.

**State machine.** The fix is a one-bit tracker
``_search_was_empty`` on ``MainWindow``, updated on every
``textChanged``. Reset fires only on **transitions**:

| Previous state | Current state | Behavior          |
|----------------|---------------|-------------------|
| empty          | non-empty     | reset (with confirm_close) |
| non-empty      | empty         | reset (with confirm_close) |
| non-empty      | non-empty     | no-op (refining search)    |
| empty          | empty         | no-op                       |

The two reset transitions are perfectly symmetric:
*empty → non-empty* (user started searching) and
*non-empty → empty* (user cleared, "starting over") are both context
changes and both drop the selection.

**Cancel-on-dirty handling.** ``editor_panel.confirm_close()`` is
called before the reset. If the user clicks Cancel:

* ``_search_was_empty`` is **already updated** to the new state
  before the cancel check. That means subsequent same-state
  keystrokes (e.g., continuing to type into a non-empty search) do
  NOT re-prompt — the transition check returns early.
* The user is only re-prompted if they make *another* context
  change (e.g., backspace all the way back to empty). That's the
  right behavior: each meaningful gesture earns one prompt.

**Why update state before the cancel check?** The alternative —
update only on success — would re-prompt on every subsequent
keystroke until the user accepted the discard. The chosen ordering
trades a slightly imperfect "search shows the typed text even
though selection is preserved" for a guarantee of "at most one
prompt per transition." The user can always Save or Refresh to
reconcile.

> **Insight.** **State-transition tracking turns "every event"
> into "every meaningful change."** The naive read of "drop the
> selection when search is non-empty" would loop the prompt; the
> transition-aware read says "drop on the gesture that *makes*
> search non-empty." It's a small distinction but disproportionately
> matters for any UI where actions have side effects (prompts, IO,
> network) — the right granularity for side-effecting handlers is
> the *transition*, not the *value*. Same pattern as edge-triggered
> vs level-triggered interrupts in systems code: edge-triggered is
> almost always what user-facing code wants.

---

### 7.28 Doubled frontmatter terminator confuses Qt's markdown parser

**Bug.** Opening the code-review skill's ``SKILL.md`` (in the
``idm-agent-skills`` marketplace) in the Preview tab rendered as a
mangled fragment of the document — the user saw lines from inside an
example code block (``reviewed:`` and ``path/to/file.py:<line>``
repeated, with most of the actual prose missing).

**Root cause: parser composition.** The file ships with a *doubled
frontmatter terminator* — an authoring slip where someone wrote
three dashes twice at the end of the YAML:

```
---                ← line 1, opens frontmatter
name: code-review
...
---                ← line 8, valid close
---                ← line 9, ORPHAN (the slip)
                   ← line 10, blank
# Python Code Reviewer
```

Our pre-processor ``_strip_frontmatter`` correctly identified lines
1–8 as the frontmatter and stripped them, leaving the orphan ``---``
on line 9 at the head of the body. We then passed that to
``QTextDocument.setMarkdown()``.

Qt's CommonMark parser reads the leading ``---`` and (per spec,
plausibly) interprets it as the *opener* of a new YAML frontmatter
block. It scans ahead for a closer. The next ``---`` line in the
file is **inside** a ``` ```markdown ``` example block further down,
where the SKILL.md describes the structure of a generated
``REVIEW.md`` file (which itself contains frontmatter).

Result: Qt hides every line between the orphan ``---`` and that
inner ``---``, which happens to be most of the document, and starts
rendering from inside the example block. The visible fragments the
user saw (``reviewed:``, ``path/to/file.py:<line>``, ``# corrected
code``) are exactly the contents of that example block, surfacing
only because the misparse broke out of the code fence at the next
``---``.

**Why this is a parser-composition bug, not a Qt bug or a file bug.**

* Our pre-processor is "correct" in isolation: it strips a valid
  paired ``---...---`` block.
* Qt's parser is "correct" in isolation: a leading ``---`` line at
  the start of a document is ambiguous between frontmatter opener
  and horizontal rule; Qt picked one interpretation.
* The file is "wrong" in isolation: doubled terminators aren't valid
  YAML.

The three components individually accept their inputs but compose
into the wrong rendered output. Fixing any one of them solves it;
we can't modify Qt's parser and we can't modify third-party
``SKILL.md`` files, so the fix has to live in our pre-processor.

**Fix.** Extend ``_strip_frontmatter`` to eat *any bare ``---``
lines that immediately follow* the frontmatter close (blanks
between them are tolerated). The loop also handles triple- or
quadruple-tap cases for free:

```python
body = md[next_newline + 1:]
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
```

Bare ``---`` lines that are NOT immediately after the frontmatter
(e.g., inside the prose) are still rendered as horizontal rules
correctly — they're never seen by this loop because we ``break`` as
soon as the first non-``---`` line shows up.

**Verified against four input shapes** (normal frontmatter, doubled
terminator, no frontmatter, blank-line-between terminators) — all
produce clean output starting with the first real content line.

> **Insight.** **Pre-processors that hand off to a downstream parser
> have to be robust to what the downstream parser does with
> *leftovers*.** It's not enough to be "correct on the input we
> claim to handle" — we also have to defend against the residue we
> leave behind, because the downstream pass will interpret it
> however it interprets it. Here, leaving an orphan ``---`` on the
> assumption "Qt will render it as a horizontal rule" was wrong;
> Qt's actual behavior was to scan ahead and consume far more than
> a horizontal rule's worth of content. Whenever you write a
> "stripper" or "cleaner" upstream of a parser you don't control,
> ask: *what does the parser do with the head of my output?* and
> design backward from there.

---

### 7.29 Always-on context: plugin name for Plugin rows, project name for Project rows

**Request.** For Plugin skills, show the plugin name alongside the
skill name. For Project skills, show the project (root folder) name.
The group header already says *"Plugin"* / *"Project"*; the user
wants to know **which** plugin / project.

**Visual format.** Middle-dot subtitle:

```
configure  ·  discord
configure  ·  imessage
configure  ·  telegram
explain-code  ·  pyCOMPS_zdu
```

The middle-dot is conventional for *"thing · subtitle"* in modern UI
typography (Material, GitHub, Linear), reads cleanly at small sizes,
and stays visually distinct from the bracketed
``[disambiguation]`` syntax (§7.18) which can still appear *after*
the context.

**Why this is different from disambiguation (§7.18).**

| Mechanism                       | Trigger              | Shape                |
|---------------------------------|----------------------|----------------------|
| Always-on context (this §)      | Plugin or Project row, regardless of collisions | ``· <plugin/project name>`` |
| On-collision disambiguation (§7.18) | Two visible rows would otherwise render identically | ``[<context-delta>]`` |

They answer different questions:

* **"What plugin owns this skill?"** — always relevant for plugin
  rows, even when there's no name collision. Now answered without
  the user having to click the row to see the path.
* **"Which of the two ``configure`` rows is this?"** — only relevant
  when there's actually a collision, and only useful when the
  always-on context doesn't already distinguish them.

**The two interact cleanly.** Once the plugin name is on every plugin
row, the disambiguator becomes mostly redundant: two ``configure``
rows from ``discord`` vs ``imessage`` are already distinguished by
the context alone. ``_build_disambiguation_map`` now checks this
explicitly:

```python
contexts = [_context_label(s) for s in group]
if len(set(contexts)) == len(group):
    continue  # contexts already distinguish all peers — no [bracket]
```

The bracketed disambiguator only earns its keep when contexts
collide too — the canonical case being the same plugin installed
under two marketplaces (e.g., ``claude-plugins-official`` and
``claude-plugins-official.staging``, where both rows render with
the same plugin context, and the marketplace folder becomes the
only meaningful differentiator).

**Project skills.** Context is ``path.parents[2].name``, the
directory containing ``.claude/`` — the same value the disambiguator
already used for project-vs-project name collisions (§7.18). Showing
it always means the user can tell which project root a skill comes
from at a glance, important when the project picker can switch
between projects under a monorepo.

**Final label layout** (per row, each segment optional):

```
<name>  ·  <context>   [<disambig>]   [<state>]
```

* ``· <context>`` — appears on every Plugin and Project row.
* ``[<disambig>]`` — appears only when contexts don't already
  distinguish the colliding peers.
* ``[<state>]`` — ``name-only`` / ``user-only`` per existing
  state-styling rules.

> **Insight.** **Always-on context narrows the job of disambiguation.**
> Before this change, the disambiguator was carrying *two* loads:
> identifying the skill's parent plugin/project AND distinguishing it
> from same-named peers. Separating those concerns (one always-on,
> one only-on-collision) makes both clearer: the always-on context
> is short and predictable, and the disambiguator falls back to a
> bracketed marketplace tag *only* in the edge case where the
> context alone isn't enough. Same general pattern as splitting a
> single "id" column into "identity" + "uniqueness" axes — they're
> coupled in the obvious cases but have different lifecycles in
> the edge cases.

---

### 7.30 Search includes the per-type context (plugin / project name)

**Spec.** Make search match against the always-on context from §7.29
on a per-type basis:

* **Plugin rows** match against ``name + plugin name`` — typing
  ``discord`` finds every skill in the discord plugin even when none
  of those skills (``configure``, ``access``) literally contain
  "discord" in their names.
* **Project rows** match against ``name + project folder name`` —
  monorepo / multi-project users can search by project.
* **Global rows** match against ``name`` only — the group header
  already implies "Global"; there's no second axis worth searching.

**Implementation.** A two-line helper computes the haystack per skill:

```python
def _search_haystack(skill: Skill) -> str:
    ctx = _context_label(skill)
    if ctx:
        return f"{skill.name} {ctx}".lower()
    return skill.name.lower()
```

The previous filter check
``self._filter_text not in skill.name.lower()`` becomes
``self._filter_text not in _search_haystack(skill)``. Everything else
in the rebuild pipeline (state filtering, type filtering,
disambiguation, sort order) is unchanged.

**The invariant: what you see is what you can search.** Building the
haystack from ``_context_label`` (the same helper that renders the
``· <context>`` subtitle on each row) means searching can never
match a token the user can't see, and every visible token is
searchable. No hidden synonyms, no "why does this match?" mysteries.

> **Insight.** **Reuse the *visible* representation as the search
> target, not a richer hidden one.** The temptation in search UIs is
> to index every available field (description, tags, path, etc.) so
> "anything the user might type" matches *something*. That sounds
> generous but it produces opaque results — rows match for reasons
> the user can't see. Tying the haystack to what the row actually
> renders gives the search a single, learnable rule: "if you see it
> on the row, you can type it; if you don't see it, you can't." The
> trade-off is fewer matches in edge cases, but the search behavior
> becomes predictable, which is the property that compounds.

---

### 7.31 Silencing Qt's QPA "Unable to open monitor interface" noise

**Symptom.** After the app runs for a while, the console fills with:

```
qt.qpa.screen: "Unable to open monitor interface to \\.\DISPLAY1:" "Unknown error 0xe0000225."
qt.qpa.screen: "Unable to open monitor interface to \\.\DISPLAY1:" "Unknown error 0xe0000225."
```

**Diagnosis.** This is **Qt-internal** logging from the Windows QPA
(Qt Platform Abstraction) screen backend, not our code. The hex code
``0xe0000225`` is Windows SetupAPI's ``CR_NO_SUCH_VALUE`` — "the
requested registry value doesn't exist." Qt periodically queries
monitor info (EDID, DPI, refresh rate) through the Windows
Configuration Manager API as part of routine screen-update polling.
The call returns ``CR_NO_SUCH_VALUE`` whenever the OS hasn't
populated the device interface property Qt asked for — which
happens during:

* Display sleep / wake transitions.
* Remote Desktop (RDP) sessions where the virtual display has no
  real monitor EDID.
* USB-C dock or external monitor unplug-replug.
* Dynamic Refresh Rate (DRR) and HDR state changes on Windows 11.
* Multi-monitor setups where one display's EDID is incomplete.

Qt's screen backend tolerates the failure (falls back to defaults
like 96 DPI), so the app continues to work. The duplicate message
("twice") is normal — Qt polls during multiple screen-update phases
(geometry-change + DPR-update), so a single sleep/wake triggers
the warning twice.

**Why this matters.** Three reasons it's worth filtering:

1. It pollutes the console for developers running from source —
   easy to mistake for an actual error.
2. It can fire many times per session if the user's setup is
   multi-monitor / RDP / power-managed.
3. Future real errors get hidden in the noise.

**Why we can't fix the underlying call.** It's deep in Qt's
``QWindowsScreenManager``, invoked via internal polling, not from
any code we control. We can't disable the poll without losing
DPR/refresh-rate updates entirely.

**Fix.** Install a Qt message handler in ``main.py`` via
``qInstallMessageHandler`` that **swallows just this one warning**
and forwards everything else to stderr:

```python
def _qt_message_handler(mode, context, message):
    if mode == QtMsgType.QtWarningMsg and \
            "Unable to open monitor interface" in message:
        return
    # Mimic Qt's default formatter for unfiltered messages.
    category = (context.category or "").strip()
    prefix = f"{category}: " if category else ""
    sys.stderr.write(f"{prefix}{message}\n")
```

The handler is installed at the very top of ``main()`` — before
``QApplication`` construction — so even early Qt warnings get
routed through it.

**Why a substring filter, not a category filter.** The blanket
alternative is ``QT_LOGGING_RULES=qt.qpa.screen.warning=false`` (or
``QLoggingCategory.setFilterRules``). That would silence the whole
``qt.qpa.screen`` warning category — including any future
warnings in that category that *would* indicate real bugs. A narrow
substring match is targeted: it filters exactly the known-cosmetic
pattern and nothing else.

> **Insight.** **Suppressing log noise is a real, valid form of
> bug-fix work — but it has to be surgical, not blanket.** The
> wrong fix here would be "silence the whole screen category" —
> that trades a small cosmetic win for a permanent blind spot. The
> right fix is to identify the *specific* message pattern that's
> known-cosmetic and filter only that, leaving the rest of the
> category audible so any future real issue still surfaces. The
> general principle: when filtering logs, prefer fingerprints over
> categories. Fingerprints don't drift; categories do.

---

### 7.32 Refresh button gets the standard clockwise-arrow icon

**Request.** Add a standard refresh / sync icon to the toolbar's
Refresh button — text-only buttons in dense toolbars are slower to
recognize than icon-decorated ones, and "refresh" is one of the
most universal pictogram conventions in modern UI.

**Implementation.** New ``refresh_icon()`` in ``ui/_icons.py``,
paired with ``QPushButton.setIcon`` in ``main_window.py``. Same
construction pattern as ``search_icon`` (§7.23):

* 32x32 physical canvas → ``setDevicePixelRatio(2.0)`` *after*
  painting so QPainter operates in physical coords. See §7.23 for
  the latent-bug rationale on ordering.
* Stroke color ``#444444`` — slightly darker than the search
  icon's ``#888888`` because the refresh button has a grey
  background, not a white text input. The icon needs to read
  against the button.
* ``Qt.RoundCap`` on the pen for the arc; arrowhead is filled
  (``setBrush(color)`` + ``Qt.NoPen``) so the triangle stays crisp
  at 16px logical size (hollow triangles look spindly at small
  scales).

**Geometry.** A ~270° clockwise arc with the gap at the top — the
standard "missing piece" convention. Arc starts at 45° (upper-right,
~1:30 clock position), spans -270° (clockwise), ends at 135°
(upper-left, ~10:30). Arrowhead at the **start** of the arc
(the upper-right end) pointing along the clockwise tangent
(down-right):

```
Qt angles: 0° = East, positive = CCW
                                 _____
                                /     \
                               |       ↘   ← arrowhead at 45°,
                               |       |     tangent down-right
                               |       |
                                \_____/
                        clockwise around the gap-at-top circle
```

**The math, decoded.** Qt's ``drawArc`` uses 1/16-degree units and
the standard math convention (0° = East, positive = CCW), but the
canvas is screen-coords (Y *down*), so position-on-circle and
tangent-direction both need a sign flip on the Y component:

```python
tip   = (cx + r*cos θ, cy - r*sin θ)   # screen coords
fwd   = (sin θ, cos θ)                 # CW tangent in screen coords
perp  = (cos θ, -sin θ)                # rotated 90° CW from forward
back  = tip - arrow_len * fwd          # base of arrowhead
wing1 = back + arrow_half * perp
wing2 = back - arrow_half * perp
```

At θ=45° the tip lands at (23.07, 8.93) and both wings stay
comfortably inside the 32×32 canvas — verified numerically before
shipping (see the smoke test in §7.32's commit).

> **Insight.** **Programmatic icons compose, asset icons don't.**
> Every icon in this app (app logo, search magnifier, refresh
> arrow, per-type skill badges) is painted by code at runtime
> rather than shipped as a PNG/SVG. That means: same DPR strategy
> everywhere, same color values pulled from one source of truth,
> recolor-on-state without re-rasterizing, and zero asset-pipeline
> overhead. The trade-off is that each icon is ~30 lines of paint
> code instead of a single SVG file — but those 30 lines are also
> the documentation for *why* the icon looks the way it does, and
> they live next to the rest of the rendering logic where someone
> editing the toolbar can find them. For UI work where icons are
> few and conventional, this is the better trade.

---

### 7.33 Busy indicator: status-bar progress bar + cursor + button disable

**Spec.** Surface "the app is doing something, please wait" feedback
when the user triggers one of three context-reset gestures:

1. **App start** — initial ``scan_all()``.
2. **Refresh button** — explicit rescan.
3. **Search-clear (X)** — drop selection and reset panels.

**Asymmetry.** Cases 1 and 2 do real I/O (file scan + YAML parse
across Global + plugin + project trees); case 3 is a UI-only reset
that completes in <10ms. Treating all three identically would lie
to the user about case 3. Different work → different feedback.

**Design considered.**

| Approach | Pros | Cons | Verdict |
|----------|------|------|---------|
| Status bar text + busy cursor | Minimal; no new widgets | Easy to miss; only motion is the cursor | Insufficient |
| Status bar progress bar + cursor + disabled Refresh | Visible motion, multi-modal, no threading | UI still freezes during scan | **Chosen** |
| Async scan in QThreadPool | UI fully responsive; most polished | Crosses the "Qt-free scanner" line (CLAUDE.md); threading complexity | Deferred |
| Modal "Loading…" dialog | Unmissable | Disruptive for sub-second work | Rejected |

**Chosen design.** A single ``_busy`` context manager on
``MainWindow`` that holds the UI in a coordinated busy state for
the duration of a block. On enter:

* Show an indeterminate ``QProgressBar`` (range = ``(0, 0)``, Qt's
  "marquee mode") permanently mounted on the right of the status
  bar — hidden by default, revealed only during ``_busy``.
* ``QApplication.setOverrideCursor(Qt.WaitCursor)`` — the
  OS-level cursor animates from the OS, not the app's event loop,
  so it keeps moving even while the main thread is blocked.
* Disable the Refresh button so the user can't trigger a second
  scan mid-way through the first. ``QPushButton.setEnabled(False)``
  also blocks the F5 keyboard shortcut wired to the same button.
* Set the status-bar message (e.g. ``"Scanning skills…"``).
* Call ``QApplication.processEvents()`` once so all of the above
  paint *before* the sync work starts. Without this flush, the
  busy indicator wouldn't render until the event loop ran again —
  which, for a sync scan, is *after* the scan completes (when the
  indicator is no longer needed).

On exit (including on exception, via ``try/finally`` inside the
context manager): cursor restored, progress bar hidden, button
re-enabled. The status text is left for the *caller* to set — that
lets each call site choose its own completion message
(``"Loaded N skills"``, ``"Scan error: …"``, etc.) without
``_busy`` having to know which path it's on.

**Initial scan deferred to after first paint.** The constructor
previously called ``self.refresh()`` directly inside ``__init__``
— which runs *before* ``window.show()`` in ``main.py``, so the
busy indicator would never get a frame to render at startup
(everything happens before the window is visible). Replaced with
``QTimer.singleShot(0, self.refresh)``, which queues the scan for
the next event-loop tick — after the show event has fired and the
empty window has painted. The user briefly sees the empty toolbar
and skill list, then the busy state kicks in, then results
populate. The brief "empty window" frame is the right signal:
"the app started, now it's loading."

**Search-clear gets a flash, not a busy state.** Since the
operation is genuinely instant, ``_on_search_changed`` shows a
2.5-second status-bar message (``"View cleared — start typing or
pick a skill"``) instead of entering ``_busy``. Treating it as a
"loading" state would lie about the work being done; a brief
acknowledgment matches the actual semantics.

**Why the indeterminate animation can freeze during sync work.**
``QProgressBar`` in marquee mode animates by repainting on a Qt
internal timer. That timer fires through the event loop, which is
blocked during ``scan_all``. So the progress bar appears (because
``processEvents`` flushed the show), but it doesn't visibly
animate during the scan — it shows up as a static partially-filled
bar. The OS-level ``WaitCursor`` covers the "I am alive" signal
during this window; the progress bar's role is to be a *visible*
status-bar element saying "operation in progress." A future
iteration (Approach C in the design table — async scan in a
QThreadPool) would let the bar animate continuously, at the cost
of threading the scanner.

> **Insight.** **A coordinated "busy state" is more than any one
> indicator.** The cursor, the progress bar, the disabled button,
> the status text, and the ``processEvents`` flush are individually
> small; together they're a multi-modal "we're working" signal
> that's hard to miss regardless of where the user is looking on
> the screen. Each indicator covers a different failure mode of
> the others (cursor: keeps moving when main thread blocks;
> progress bar: visible at-a-glance widget; button disable:
> prevents double-trigger; status text: tells the user *what*
> is happening). The right pattern for "UI feedback during a
> sync operation" is rarely a single widget — it's a small
> bundle of complementary cues, all toggled together.

---

## 8. Cross-Cutting Patterns

Several patterns recur across the iterations and are worth preserving as
project conventions:

### 8.1 Lazy model attachment for "blank" states

`FileTreePanel` doesn't `setModel(QFileSystemModel)` until first use. Empty
state and clear() both achieve "no display" via `setModel(None)`. Avoids
sentinel paths and Windows' "drives list" leak.

### 8.2 Per-panel `clear()` orchestrated by parent

Every panel exposes `clear()`. `MainWindow.refresh()` orchestrates:
`confirm_close()` → null `_current_skill` → `file_tree.clear()` →
`editor_panel.clear()` → rescan → repopulate. No panel reaches into another's
internals.

### 8.3 Signals up, methods down

Cross-panel coordination uses Qt signals routed through `MainWindow`
(`skill_selected`, `file_activated`, `file_saved`). Direct cross-panel
references would couple panels and make any of them un-relocatable.

### 8.4 Domain modules are Qt-free

`scanner.py`, `skill_md.py`, `models.py` — no PySide6 imports. Provable by
running them from a plain Python script. This is what allowed the
end-to-end scanner sanity check to run before the first GUI launch.

### 8.5 Stylesheets scoped per widget

Tab styles on `self.tabs`. Tree styles on `self.tree`. No global
`app.setStyleSheet`. That keeps native theming for everything else and
prevents a single style change from cascading across unrelated widgets.

### 8.6 Content-based dirty over flag-based

Dirty is `buffer != pristine`. Pristine snapshot updated on
open/save/revert. Comparison runs on `textChanged`. Immune to
syntax-highlighter rehighlights AND to "type back to original."

---

## 9. Bonus / Future Extensions

The architecture intentionally puts the seams in obvious places. Each item
below lands in exactly one or two files.

| Feature | Where |
|---|---|
| Dark mode | Stylesheet swap in `MainWindow._restore_settings`; highlighters use named colors |
| JSON export | `dataclasses.asdict` over `skill_list._all_skills` (Path → str trivially) |
| Plugin-level disable button | `SkillInfoPanel` State row when `skill.plugin_id is not None`: a "Disable plugin" button that confirms the blast radius, then writes `enabledPlugins[skill.plugin_id] = false` to `~/.claude/settings.json` via `skill_settings`. Per-skill plugin toggling is intentionally NOT supported (see §7.14 decision 3); this is the plugin-level alternative. |
| Git status overlay | Per-`Skill.path` `git status --porcelain` once; decorate tree item icon; cache, refresh on save |
| Live frontmatter sync to left panel | New `EditorPanel.skill_meta_changed` signal → forwarded by `MainWindow` → `SkillListPanel` reapplies the rename |

---

## 10. How to Run

```powershell
cd C:\work\claude_demo\Claude_Skills_Manager_GUI
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

First scan finds Global + Plugin skills under `~/.claude/`. Click
**Choose…** in the toolbar to add a project root for recursive
`.claude/skills/` discovery.

---

## 11. Files Inventory

| File | Lines (approx) | Role |
|---|---|---|
| `main.py` | 98 | Boots `QApplication` + `MainWindow`; registers Windows AppUserModelID (§7.22) so the taskbar shows the app's icon, not python.exe's; installs a Qt message-handler filter that suppresses the cosmetic `qt.qpa.screen` "Unable to open monitor interface" warning (§7.31) while passing everything else through to stderr |
| `claude_skills_manager/models.py` | 39 | `Skill` dataclass (+ state, plugin_id) + `SkillType` enum |
| `claude_skills_manager/skill_md.py` | 85 | YAML frontmatter + first-paragraph parser; chars-per-token estimator |
| `claude_skills_manager/skill_settings.py` | 134 | `skillOverrides` + `enabledPlugins` read/write; refuses to overwrite malformed JSON |
| `claude_skills_manager/scanner.py` | 321 | 3-source discovery (manifest-driven plugin discovery + legacy fallback), recursive project walk, dedup, state-population pass (per-skill scope) |
| `ui/main_window.py` | 569 | Toolbar (logo + Type + State filter groups, rich-text section labels, leading-magnifier search box per §7.23, refresh-icon button per §7.32), nested splitters, signal routing, `QSettings` persistence; validate-before-mutate on Choose-root; image-vs-text dispatch in `on_file_activated`; restores file-tree selection on cancelled-discard (§7.25); search empty↔non-empty transition resets selection (§7.26 + §7.27); `_busy` context manager coordinates cursor + indeterminate progress bar + disabled-button busy state, initial scan deferred to first paint via `QTimer.singleShot` (§7.33); binds Windows taskbar icon via per-window IPropertyStore in `showEvent` (§7.22) |
| `ui/win32_taskbar.py` | 198 | Win32-only: per-window AppUserModelID + RelaunchIconResource binding via `SHGetPropertyStoreForWindow` and the `IPropertyStore` COM vtable. Pure ctypes (no comtypes / pywin32), silently no-ops on non-Windows or COM failure — see §7.22 |
| `ui/skill_list.py` | 610 | Grouped tree with type + state + per-type-context search filtering (§7.30), selection styling, per-type badge icons (full + faded variants — see §7.19/§7.20), right-click copy/open/Enable/Disable menu; **always-on plugin / project context** as `name · context` (§7.29) plus on-collision name disambiguation scoped per group header (§7.18); tooltip enrichment for plugin-off, in-place `refresh_state`; correct paint-then-tag-DPR ordering (§7.23); programmatic `clear_selection()` for search-clear reset (§7.26) |
| `ui/file_tree.py` | 79 | `QFileSystemModel`-backed tree, lazy attachment, `select_path()` for programmatic selection-restore on cancelled-discard (§7.25) |
| `ui/skill_info_panel.py` | 218 | SKILL.md size / mtime / line / char / token-estimate + Enable/Disable buttons in title row, word-wrapped form, plugin / non-binary read-only states |
| `ui/editor_panel.py` | 455 | Skill Description preview (+ description token estimate) + editor + conditional `.md` Preview tab (§7.24), content-based dirty, save/revert; `open_file` returns bool + `current_path()` accessor for cancel-discard selection-restore (§7.25); state-driven tab visibility via `_apply_tab_visibility` — Skill Description shown when a skill is selected, Editor added on file open, Preview added only for `.md` (§7.26); `_strip_frontmatter` eats orphan `---` lines following the close to defend against doubled terminators in third-party SKILL.md files (§7.28) |
| `ui/image_dialog.py` | 229 | Modal QGraphicsView image viewer — Ctrl+wheel zoom (cursor-anchored), drag-pan, Fit/100%/+/− toolbar, keyboard shortcuts |
| `ui/code_editor.py` | 98 | `QPlainTextEdit` + line numbers + current-line highlight |
| `ui/syntax.py` | 94 | Python / JSON / Markdown highlighters |
| `ui/_styles.py` | 38 | Shared QSS constants (currently `BUTTON_STYLE`); single source of truth for per-widget stylesheet snippets, per the §8.5 "stylesheets scoped per widget" convention |
| `ui/_icons.py` | 154 | Programmatic small UI icons — `search_icon()` (§7.23) and `refresh_icon()` (§7.32, standard clockwise-arrow with filled arrowhead). Separate from `app_icon.py` which is the brand logo. Same paint-then-tag-DPR pattern across all icons |
| `ui/app_icon.py` | 132 | Programmatic three-shapes composite logo (circle + square + diamond in the per-type palette); three layers over a shared physical-pixel painter — `app_icon()` for window-icon multi-size pack, `app_logo_pixmap(logical_size)` for in-window toolbar use with HiDPI DPR, `write_logo_ico(path)` for on-disk .ico used by the Windows taskbar binding — see §7.21, §7.22 |
