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

### 7.34 Skill Test dialog — modeless, CLI-backed, per-skill

**Spec.** Add a UI for testing a selected skill end-to-end against an
ad-hoc prompt. The dialog should surface basic skill metadata, the
rendered SKILL.md content, the skill's examples, the raw file, and a
test-runner pane where the user types a prompt, fires a request to
Claude, and sees the response. Open via a toolbar button, a
right-click context-menu entry, and a keyboard shortcut.

**Opening mechanism — modeless QDialog.**

| Approach | Pros | Cons | Verdict |
|----------|------|------|---------|
| Modeless `QDialog` (`show()` not `exec()`) | Doesn't block main window during multi-second Claude calls; multiple skills can be tested in parallel windows; clean separation from the existing 3-pane layout | New window class to manage | **Chosen** |
| 4th tab on right panel | Single window | Squeezes existing tabs; complicates the §7.26 no-skill / no-file state machine; test runner needs vertical space the editor panel doesn't have | Rejected |
| New 4th main panel | Always visible | Crowds the 3-pane layout; mostly wasted space when not testing | Rejected |
| Modal dialog | Simplest | Blocks main window during long Claude calls — user can't reference other skills mid-run | Rejected |

The dialog opens from three converging entry points: a toolbar
**Test Skill…** button (with the new flask icon), the **Ctrl+T**
shortcut on that button (auto-surfaces in tooltip), and a **Test
Skill…** entry at the top of the skill-list right-click context
menu. All three call ``MainWindow.open_test_dialog(skill)``.

**Test execution — `claude` CLI via `QProcess`.**

| Approach | Pros | Cons | Verdict |
|----------|------|------|---------|
| Shell out to `claude` CLI | Skills resolve via the user's existing Claude Code config (``enabledPlugins`` + ``skillOverrides``) — the test runs against the *same* state the user is managing in this app; no new auth; no new deps; streaming via Qt signals | Requires `claude` on PATH | **Chosen** |
| Anthropic SDK direct API call | Most control | Adds `anthropic` dep; needs API-key handling; have to manually inject skill context (replicates Claude Code's resolution logic — fragile) | Rejected |
| Copy prompt to clipboard | Zero new infra | Doesn't actually test anything | Rejected |

The CLI invocation is ``claude -p <prompt>`` — Claude Code's
non-interactive print mode. ``QProcess`` is preferred over plain
``subprocess.Popen`` because it integrates with the Qt event loop:
``readyReadStandardOutput`` fires per chunk, so streaming works
without a worker thread, and ``finished`` / ``errorOccurred`` deliver
exit-code and error info as signals. The argv-list form
(``start(program, args)``, not the shell-string form) sidesteps
quoting issues for prompts containing quotes, backticks, or dollar
signs.

Working directory is the project root for **Project** skills (so any
project-scoped ``settings.local.json`` takes effect) and
``Path.home()`` for **Global** / **Plugin** skills (no project
context to honor).

**Multi-instance — one dialog per skill.** ``MainWindow`` holds a
``_test_dialogs: dict[Path, TestSkillDialog]`` keyed on the skill's
absolute path (the same dedup key the scanner uses). Re-opening for
the same skill calls ``raise_() + activateWindow() + show()`` on the
existing dialog instead of constructing a new one. Different skills
get separate dialogs in parallel.

Lifecycle: the dialog is constructed with ``WA_DeleteOnClose`` so Qt
destroys it when the user closes the window. Before destruction the
dialog emits ``closed`` (Signal carrying ``skill.path``); the parent
slot ``_on_test_dialog_closed`` removes the entry from the registry
map. No explicit ``hide()``/``deleteLater()`` plumbing in
``MainWindow`` — Qt and the close handler do the work.

**Dialog layout (top-down).**

1. **Header strip** — name (large bold) + plugin/project context
   suffix in faded grey; one-line description below (italic);
   metadata line (type, state, ~token count, modified date); path
   in monospace. Rich-text label rather than a grid so it wraps
   gracefully and stays selectable as a single block.
2. **Tabs** — Description / Examples / Raw SKILL.md / Test.
3. **Close button** — bottom-right, escape shortcut, no other dialog
   chrome.

The Description tab uses the same rendering approach as the main
window's Skill Description tab: ``strip_frontmatter`` then
``setMarkdown`` on a ``QTextBrowser``. The Raw tab uses the existing
``CodeEditor`` + markdown highlighter, set to read-only (rather than
``QTextBrowser``) so the syntax highlighter still runs against the
document.

**Test runner — vertical splitter, plain-text response.** Prompt
input (``QPlainTextEdit``, multi-line) on top; control row (Run /
Cancel / Clear / status / busy bar); response viewer (``QPlainTextEdit``,
read-only, monospace) on bottom. The split is a ``QSplitter(Vertical)``
so the user can rebalance — default sizes give the response ~75% of
the vertical space because that's where the volume lives.

Response is rendered as plain text, not markdown, deliberately:
during streaming, reflowing a markdown render on every chunk is
visually janky and obscures the streaming-as-it-arrives feel. Plain
text matches what the user would see in their own terminal running
the same command.

**Process-lifecycle defenses.** ``QProcess`` has a few sharp edges
that the dialog explicitly handles:

* **FailedToStart fires alone.** When ``claude`` isn't on PATH the
  process never starts; ``errorOccurred`` fires with
  ``QProcess.FailedToStart`` but ``finished`` does **not** follow.
  ``_teardown_process`` is called from both ``_on_finished`` AND
  ``_on_error`` (FailedToStart branch) so the run-state reset
  happens in every path.
* **Crashed fires twice.** When the process is killed (cancel) or
  segfaults, both ``errorOccurred(Crashed)`` and
  ``finished(_, CrashExit)`` fire. The error handler is a no-op for
  ``Crashed`` to keep the run reset on a single path
  (``_on_finished``).
* **Cancel vs. crash disambiguation.** ``_was_cancelled`` flag is
  set in ``_on_cancel`` before calling ``kill()``; ``_on_finished``
  uses it to label the status correctly (``"Cancelled after Xs"`` vs
  ``"Process crashed after Xs"``).
* **Partial-line streaming.** ``readyReadStandardOutput`` fires per
  *chunk*, which may be a fragment of a line. ``_append_response``
  uses cursor manipulation + ``insertText`` instead of
  ``appendPlainText`` (which would insert a newline before each
  call, fragmenting line boundaries).
* **Final-drain on exit.** ``_on_finished`` calls ``_on_stdout`` /
  ``_on_stderr`` one more time before computing duration; on some
  platforms a final flush of the child's stdout lags the kernel
  exit notification, and we don't want the last chunk of the
  response to be missing.
* **Close mid-run.** ``closeEvent`` sets ``_was_cancelled = True``,
  kills the process, then waits up to 1.5s for it to exit (so we
  don't block UI shutdown if it's wedged). Combined with
  ``WA_DeleteOnClose`` the dialog disappears and the QProcess gets
  ``deleteLater``'d via parent-child ownership.

**`strip_frontmatter` promoted from `editor_panel` to `skill_md`.**
The §7.28 doubled-`---` defense is now imported from
``skill_md.strip_frontmatter`` by both the editor panel's Preview
tab and the test dialog's Description tab. Previously
module-private inside ``editor_panel`` — duplicating it into the
new dialog would have split the defense across two copies that
could drift apart. Moving it to ``skill_md`` (Qt-free, naturally a
home for string-level SKILL.md parsing) keeps the rule that
"defenses against malformed authoring live in exactly one place"
true. Editor panel keeps its `_apply_tab_visibility` and other
logic untouched.

**Layering kept clean.** New file ``skill_introspect.py`` is
Qt-free — it imports nothing from PySide6, only ``Skill`` /
``SkillType`` from ``models`` and ``Path`` / regex / sys. Holds the
example-extraction, summary-extraction, CLI-command-construction,
and working-directory-decision logic. The dialog imports from it
but never the other way around. If a future iteration needs to add
e.g. an Anthropic-SDK runner (Approach 2 from the table), this is
where the runner-shape lives; the dialog stays a presentation layer.

**No history.** Each Run replaces the response area; no persistent
log of prior prompts. Matches the lightweight tone of the rest of
the app. If the user wants to iterate on a prompt, the prompt
itself stays in the input until they hit Clear, so re-runs are
edit-and-rerun.

> **Insight.** **The seam between "Qt-aware lifecycle" and
> "Qt-free parsing" pays off here.** `skill_introspect.py` is
> small but carries every decision a future runner needs — what
> argv to use, what cwd, what counts as an example — and a unit
> test against it doesn't need a QApplication. The dialog stays
> a presentation layer: it owns the QProcess + signals + widgets,
> but the *shape of the invocation* (and the cancel/error
> defenses sit in pure-Qt territory). Each layer's failure mode
> is narrow: parsing bugs surface independently of UI bugs.

### 7.35 Test runner: visibility into the "no output yet" window

**Symptom.** User reported running a prompt like ``"use 'skill-search'
skill to find 'review' related skills"``, seeing the status label
freeze at ``"Starting…"``, and never receiving a response — "no log
or other information to check the status."

**Root cause.** ``claude -p <prompt>`` in its default output mode
(``text``) **buffers the full assistant response** and emits it to
stdout only when the model finishes generating. There is no
streaming; the child process is doing work but no bytes flow through
the pipe for the entire "thinking" window — easily 10–60 seconds for
complex skill-routing prompts. From the dialog's perspective
``readyReadStandardOutput`` simply never fires, so ``_on_stdout``
never runs, so the status label is never updated. The pane looks
frozen even though the QProcess is perfectly healthy.

This isn't a bug in QProcess or in our wiring — it's an honest
consequence of the CLI's output model. The fix is to stop relying
on stdout as the only signal that "something is happening."

**Fix.** Four coordinated changes in ``test_dialog.py``:

1. **A `QTimer` ticking every 500ms** runs while the process is
   alive. Its slot reads ``time.monotonic() - run_started_at`` and
   rewrites the status label whether or not stdout has fired —
   the label now ticks visibly even during the silent thinking
   window, proving the dialog is alive.
2. **Status text is bimodal**, driven by a ``_received_bytes``
   counter that's bumped on every stdout/stderr chunk:
   * ``"Running… 12.4s · waiting for output…"`` while
     ``_received_bytes == 0``. The "waiting" phrasing reassures the
     user that silence is expected, not a freeze.
   * ``"Streaming… 27.8s · 1,247 bytes"`` once any data has
     arrived — the byte count gives a sense of progress for large
     responses.
3. **A diagnostic preface is appended to the response pane at the
   start of every run** so the user can see exactly what's being
   executed:
   ```
   [cmd] claude.cmd -p "use 'skill-search' skill to find …"
   [cwd] C:\Users\zhaoweidu
   [waiting for output — `claude -p` buffers the full response
   by default, so nothing arrives until the model finishes]
   ```
   Repurposing the response pane for status info (rather than
   adding a separate "diagnostics" widget) keeps the dialog
   chrome unchanged and means the diagnostic + the response stay
   in chronological order — the user reads top-to-bottom and
   sees the full timeline.
4. **`QProcess.started` is wired** so the moment the OS confirms
   the child is live, the status flips from ``"Starting…"`` to
   ``"Running… 0.0s · waiting for output…"``. Distinguishing
   "start() was called" from "child has exec'd" surfaces an OS
   delay (e.g. AV scanning the `.cmd` shim on Windows) that would
   otherwise look identical to a hung process.

**Closing markers in the response pane.** Each terminal state
appends a one-line bracketed summary so the user can see the
result without looking up at the status label:
``[done in X.Xs · N bytes received]`` / ``[cancelled by user
after X.Xs]`` / ``[process crashed after X.Xs]`` / ``[exited with
code N after X.Xs]``. Status label has the same info, but the
pane is where the user is already looking.

**Tick-timer hygiene.** The timer is created once in ``__init__``,
started in ``_on_run`` after ``QProcess.start()``, and stopped in
``_teardown_process``. The slot guards ``_process is None`` so a
stale tick already in the event queue at teardown time is a no-op
rather than a crash.

**Why not switch to `--output-format stream-json`?** That mode
*does* stream events incrementally and would fix the visible
silence directly. It was rejected for this iteration because:

* It would require a streaming JSON parser (line-by-line, with
  partial-message buffering) in the dialog, which is real
  complexity for a feature that's not yet load-bearing.
* The tick-timer approach is honest about what's happening even
  for OTHER long-running CLI tools the user might want to test
  in the future — it doesn't assume a specific output format.
* If the user wants streaming output, they can switch
  ``build_claude_command`` in one place (Qt-free domain code) and
  add a parser; the dialog layer doesn't change.

> **Insight.** **"No output" is not the same as "no progress".**
> When the only feedback channel is stdout, a process that's
> working hard but not yet ready to emit looks identical to a
> hung one. Adding an *independent* signal (in this case a clock
> ticking via QTimer) and a clear hint of what the user is waiting
> for ("waiting for output…") collapses the two cases into one
> obviously-alive UI. The general pattern: when external code
> defines your visibility, layer your own visibility on top.

### 7.36 "Check Claude" baseline health check — isolating variables

**Symptom continued from §7.35.** Even with elapsed-time visibility,
the user reported the test still appeared to hang indefinitely. The
visibility fix solved "the dialog looks frozen" but didn't tell the
user *why* the actual CLI was unresponsive. From the dialog's
perspective the run is genuinely stuck — but is the cause the CLI
install, the skill being tested, the prompt itself, or QProcess
plumbing?

**Diagnostic principle: isolate variables.** A skill-test run
exercises THREE things at once:

1. The user's ``claude`` CLI install (auth, network, model access).
2. The specific skill being tested (its description-match, any
   tools it triggers).
3. The user's prompt (which may inadvertently send the model into
   a long tool-using loop).

When such a run hangs, the user can't tell which axis is broken. A
baseline check removes axes (2) and (3) by sending a fixed, trivial
prompt with no skill dependency. If the baseline check works, the
problem is skill-or-prompt-specific; if it hangs the same way, the
issue is the CLI install itself.

**Implementation.** New modeless dialog ``CheckClaudeDialog`` in
``ui/check_claude_dialog.py``, opened by a new **Check Claude**
toolbar button:

* Fires ``claude -p "Reply with exactly the two characters: OK"``
  on open (auto-run via ``QTimer.singleShot(0, …)`` so the window
  paints first).
* 60-second hard timeout via a single-shot ``QTimer`` — a
  2-character response should never legitimately take longer; past
  this the run is killed and labeled FAILED.
* Reuses the §7.35 visibility pattern (500ms tick timer + bimodal
  status text + diagnostic preface in the output pane), plus a
  countdown to the timeout (``"timeout in 47s"``) so the user
  knows how long until verdict.
* Status label changes color on completion: green for success,
  amber for "succeeded with no output" (a soft warning), red for
  FAILED states. The colored status is the at-a-glance verdict; the
  output pane is for the details.
* **"Copy Command" button** puts the exact ``claude -p
  "<prompt>"`` invocation on the clipboard so the user can paste
  it into a terminal and see what happens *outside* our QProcess
  wrapper. This is the killer diagnostic move: if the terminal
  hangs the same way, the issue is in ``claude``'s setup (most
  likely first-time auth needing interactive login); if the
  terminal works but our dialog doesn't, it's a QProcess wrapping
  bug worth filing.

**Singleton-ish dialog.** ``MainWindow`` keeps a single
``_check_claude_dialog`` reference (not a dict like
``_test_dialogs``) because there's no per-skill axis — there's
only one ``claude`` install. Re-clicking the toolbar button raises
the existing window. Same WA_DeleteOnClose + ``closed`` signal
pattern as ``TestSkillDialog``.

**Why not auto-check the response text?** ``claude``'s response to
``"Reply with OK"`` could be "OK", "OK.", "OK\n", "OK!", "OK 👍",
"Sure, OK", or markdown-formatted variants. Auto-grepping for
"OK" would either be lenient (false positives) or strict (false
negatives). Instead, the dialog reports "completed with N bytes,
verify the response below looks reasonable" and lets the user
judge. The dialog's purpose isn't to grade Claude — it's to tell
the user whether `claude` is producing *any* response at all.

**Why this is separate from `TestSkillDialog` rather than inline
in it.** The Test Skill dialog is per-skill (registry keyed on
``skill.path``) — folding the baseline check in would either need
a separate entry point inside every test dialog instance (which is
duplication) or a "no skill" mode for that dialog (which violates
its purpose). Keeping the health check standalone, on the main
toolbar, makes it discoverable independent of whether the user
has a skill selected.

**File layout.** ``CheckClaudeDialog`` and ``TestSkillDialog``
share the same QProcess wiring shape (tick timer, byte counter,
WA_DeleteOnClose, signal teardown), so a future iteration could
factor a ``ClaudeRunner`` helper to deduplicate. For now the
duplication is ~50 lines and the two dialogs have meaningfully
different status semantics (countdown vs. open-ended; bimodal
verdicts vs. simple done/cancel/error). Refactor when a third
caller emerges.

> **Insight.** **A "does X work at all?" button is one of the
> highest-leverage UI affordances you can add.** It costs little
> to build, surfaces only when the user is already troubleshooting,
> and replaces the question "why doesn't this work?" with the
> sharper question "which layer fails?". The Copy Command button
> on top of that is the second-derivative move: it gives the user
> a way to test *outside* your app, which is the only way to prove
> whether your wrapping is the problem. Building troubleshooting
> tools alongside features pays off the first time someone hits a
> dependency issue you didn't anticipate.

### 7.37 Health check, redesigned — two-step + silent-swallow defense

**Symptom.** With the §7.36 initial dialog the user clicked Check
Claude and saw: status label stuck at "Idle", "Run Again" button
disabled, output pane empty. No "Starting…", no diagnostic preface,
no error. The dialog was demonstrably half-toggled (button disabled
implies ``_on_run`` started executing) but produced zero feedback.

**Diagnosis.** Two issues compounded:

1. **PySide6 silently swallows exceptions raised inside queued
   slot calls.** ``QTimer.singleShot(0, self._on_run)`` queues
   ``_on_run`` on the event loop. If any line inside ``_on_run``
   raises — even something as innocuous as a transient
   ``QTextCursor`` operation on a not-yet-fully-realized widget —
   the exception goes through Qt's internal slot handler, which
   on PySide6 silently drops it on the floor. The synchronous
   work that ran before the exception (e.g. ``run_btn.setEnabled
   (False)``) takes visible effect; anything after it
   (``status_label.setText("Starting…")``, the QProcess setup,
   the diagnostic preface) silently never runs. The result is
   exactly the half-toggled UI we saw.

2. **The default test was too ambitious for a baseline check.**
   ``claude -p <prompt>`` exercises auth + network + the model —
   all the layers that the user is *already trying to diagnose*.
   If any of those is broken the check appears to hang. The user
   correctly intuited that a simpler test (``claude --version``)
   would be more useful: it exercises only the binary itself and
   completes in well under a second.

**Redesign.** Three coordinated changes:

* **Two-step dialog.** Step 1 is ``claude --version`` (auto-runs
  on first ``showEvent``, 10s timeout); Step 2 is ``claude -p
  "Reply with OK"`` (manual button, 90s timeout). Each step has
  its own status label (color-coded: blue running / green DONE /
  amber DONE-no-output / red FAILED / grey cancelled) and its
  own button. The output pane is shared and shows the most-recent
  run's output — keeping it singular avoids confusing scroll
  state with two streams interleaving.

  **Layered diagnostic:**
  | Step 1 | Step 2 | Means |
  |--------|--------|-------|
  | FAILED | not run | CLI binary is broken (not on PATH, corrupted install) |
  | PASSED | FAILED timeout | Binary OK; auth/network/model layer is broken — likely first-time auth needing interactive setup |
  | PASSED | FAILED exit code | Binary OK; ``claude -p`` itself rejected the command (rare) |
  | PASSED | PASSED | Everything works — original hang is skill-specific |

* **Auto-run moved from `QTimer.singleShot(0, …)` to `showEvent`.**
  ``showEvent`` fires deterministically after Qt has rendered the
  window. The widgets are guaranteed initialized; a queued
  ``singleShot`` slot might run against a half-built dialog on
  some Qt/PySide6 versions. ``showEvent`` also fail-safes
  correctly: if construction raised, the dialog never shows and
  the auto-run never fires (compared to ``singleShot`` which
  would queue regardless).

  A guard flag ``_auto_run_done`` ensures the version check only
  runs on the *first* show, not on subsequent shows (re-focus,
  un-minimize, etc.).

* **`try/except` around `_start_run`** with ``traceback.format_exc
  ()`` writing the full stack into the output pane on failure.
  Any synchronous error during process setup now lands in the
  pane as visible text instead of being swallowed. The same
  pattern wraps ``_on_stdout`` / ``_on_stderr`` because cursor
  manipulation can also fail in rare corner cases.

**Why isolate the variables this way.** The user's stuck-test
symptom could have been any of:

* `claude` not installed → caught by Step 1 (FailedToStart).
* `claude` installed but binary broken / version mismatch →
  caught by Step 1 (non-zero exit / crash).
* Binary OK but no auth configured → Step 1 passes, Step 2 hangs
  on the first network/auth call until timeout.
* Auth OK but network blocked / rate-limited → Step 1 passes,
  Step 2 may eventually return an error from the API.
* Everything OK at baseline → the original skill-test hang is
  skill- or prompt-specific (consider tool-use loops, very long
  context, malformed SKILL.md description matching).

Without the two-step split, all of those collapse into "the test
hangs" with no way to distinguish them. With it, the verdict on
each step localizes the failure to one layer.

**File layout.** Kept in ``ui/check_claude_dialog.py`` (now ~597
lines) rather than splitting into per-step modules. The two
steps share the QProcess wiring, the tick timer, the timeout
timer, the output pane, and the teardown logic — splitting them
would require all of those to live somewhere shared, and the
total size doesn't yet justify the indirection. Refactor when a
third step emerges or the dialog grows past ~800 lines.

> **Insight.** **When a UI behavior is impossible-looking
> ("button is disabled but status didn't change"), the framework
> is hiding something from you.** Silent swallowing of slot
> exceptions is one of those framework behaviors — small and
> subtle in isolation, dangerous when it lands at a moment where
> the user has no other signal. The defense is twofold: wrap
> async-entrypoint code (slots, timer callbacks) in
> ``try/except`` that visibly logs, and prefer
> deterministically-timed lifecycle hooks (``showEvent``) over
> "fire on next tick" idioms when the cost of the former is no
> higher than the latter.

### 7.38 Don't trust a hardcoded executable name — resolve it

**Symptom.** With the §7.37 redesigned dialog the user ran both
steps and got: *"FAILED: 'claude.cmd' is not on your PATH (or could
not be launched)"* — for both Step 1 and Step 2. But the user
demonstrably HAD `claude` installed and working (the GUI was
already managing their installed skills).

**Root cause.** The code had a hardcoded
``CLAUDE_EXECUTABLE = "claude.cmd"`` on Windows. The user's actual
install was at ``C:\Users\zhaoweidu\.local\bin\claude.EXE`` —
**a `.EXE`, not a `.cmd`**. ``QProcess.start("claude.cmd", args)``
asked Windows to launch `claude.cmd`, no such file existed, and
``FailedToStart`` fired regardless of how well-formed everything
else was.

This is a recurring class of bug when interfacing with CLIs across
platforms: **the executable extension varies by install method**.
A given user might have any of:

* ``claude.cmd`` — npm-global install (the npm shim)
* ``claude.exe`` — pyinstaller / native installer / standalone build
* ``claude.bat`` — alternative npm shim on older Node
* ``claude.ps1`` — PowerShell-only shim
* ``claude`` (no extension) — Linux/Mac, or Cygwin/MSYS on Windows

Hardcoding any one of these is wrong for some user. The right
primitive is **on-disk resolution**: locate the file ourselves and
hand QProcess a fully-qualified path, removing Windows
``CreateProcessW`` PATH search behavior from the equation entirely.

**Fix.** Two new functions in ``skill_introspect.py``:

* ``find_claude_executable() -> str | None`` — tries
  ``shutil.which("claude")`` first (which is **PATHEXT-aware** on
  Windows, so it finds whichever extension exists), then falls back
  to probing ``_WINDOWS_CLAUDE_CANDIDATES`` — a tuple of common
  install locations including ``%APPDATA%\npm\``, ``%USERPROFILE%
  \.local\bin\`` (where the affected user's install lived),
  ``%LOCALAPPDATA%\AnthropicClaude\``, and the standard installer
  paths. Returns the first existing file, or ``None`` if nothing
  matches.
* ``claude_path_diagnostic() -> str`` — assembles a human-readable
  report of *exactly what we tried and what's on PATH*. Surfaced
  in the health check dialog's output pane on ``FailedToStart`` so
  the user immediately sees:
  - what ``shutil.which`` returned,
  - which common install locations exist on disk (and which don't),
  - the full PATH inherited by the Python process,
  - concrete fix steps (install, fix PATH, restart app).

**Both dialogs updated.** ``build_claude_command`` in
``skill_introspect.py`` now returns
``[<resolved_path>, "-p", prompt]``, so ``TestSkillDialog``
automatically benefits. ``CheckClaudeDialog`` also calls
``find_claude_executable`` directly in its Step 1/2 entrypoints
and surfaces the resolved path in a **colored banner at the top
of the dialog** (green when found, red when not) — the user knows
before running anything whether ``claude`` was located.

**Why resolve on every call, not cache.** The cost is microseconds
(``shutil.which`` + a few ``Path.is_file()`` probes). Caching
means: install claude after opening the dialog → first run fails
"not found" → re-click Run after restarting the dialog → still
"not found" because the cache stuck. The fresh probe makes the
fix-and-retry workflow work without requiring an app restart in
the install-during-runtime case.

> **Insight.** **Interfacing with external CLIs is fundamentally
> a path resolution problem, not a process launch problem.** Once
> you have the full path to the binary, ``QProcess`` / ``subprocess``
> behave identically across platforms. Without it, you're at the
> mercy of OS-specific search behaviors that differ between cmd.exe,
> CreateProcessW, ``execvp``, and the various shims they wrap.
> The fix is always the same: locate the binary yourself (Python's
> ``shutil.which`` is the standard primitive, PATHEXT-aware on
> Windows), pass the full path, and treat "not found on disk" as
> an actionable error with a helpful diagnostic — not an opaque
> launch failure.

### 7.39 Close stdin, add timeout — the Test Skill hang

**Symptom.** With the §7.38 path resolution fix, ``Check Claude``
Step 2 (running ``claude -p "Reply with OK"``) completed
successfully. But ``Test Skill`` running ``claude -p "hello"``
still hung — no response, the elapsed-time tick kept rising, the
``claude`` process kept running, nothing ever drained.

**Two hypotheses, both plausible:**

1. **stdin EOF.** ``claude -p`` may read from stdin so the caller
   can extend the prompt via a pipe (``echo "more context" |
   claude -p "<args>"``). ``QProcess`` connects the child's stdin
   to a pipe by default, and we never wrote to it. Without an
   explicit EOF, ``claude`` could be blocked on a read that will
   never return.
2. **No timeout.** ``CheckClaudeDialog`` Step 2 has a 90-second
   timeout — if it hung, it would terminate. ``TestSkillDialog``
   had no timeout at all, so a slow-but-eventually-completing
   ``claude -p`` would look identical to a hard hang.

**Fix.** Two changes applied to both dialogs (mirrored where the
behavior should match):

1. **`closeWriteChannel()` immediately after `start()`.** Sends
   EOF on the child's stdin, telling ``claude`` "no piped input
   is coming, commit to processing the argv prompt alone."
   Defensive in `CheckClaudeDialog` (which works without it) and
   load-bearing in `TestSkillDialog` (the suspected stdin-wait
   case).

2. **3-minute timeout in `TestSkillDialog`.** ``_TEST_RUN_TIMEOUT_MS
   = 180_000``, enforced by a single-shot ``QTimer`` started right
   after ``start()`` and stopped in ``_teardown_process``. Past
   this point the run is killed and labeled ``TIMED OUT`` with
   troubleshooting hints embedded in the response pane:
   ```
   If `claude` consistently takes longer than this, try:
     * A simpler / more directive prompt
     * Test the prompt manually in a terminal: claude -p "<prompt>"
     * If the terminal hangs too, `claude -p` may be waiting on
       tool-permission input —
       consider running with `--dangerously-skip-permissions` for testing
   ```
   3 minutes is generous for typical round-trips (5-60s) while
   keeping the dialog from looking permanently hung. The user can
   still hit Cancel at any time for an earlier exit; the timeout
   is a backstop.

**Why both fixes vs. just one.** stdin-close is a *correctness*
fix — the child should know we're not piping input. Timeout is a
*robustness* fix — even if stdin-close didn't matter and the hang
came from a tool-permission wait or rate-limit backoff, the
timeout still produces a verdict instead of an infinitely-running
UI. Each defense covers a different class of failure mode.

**FailedToStart diagnostic now in TestSkillDialog too.** Previously
only `CheckClaudeDialog` rendered the full PATH diagnostic on
launch failure; ``test_dialog.py`` had a generic "not on PATH"
message. Both now use ``claude_path_diagnostic()`` (§7.38) so the
user gets the same actionable report regardless of which dialog
surfaced the failure.

**`--dangerously-skip-permissions` mention.** Surfaced only in the
timeout-failure message as a *suggestion to try manually*, not
added automatically to our invocation. Skills that exercise tools
need permission grants; in ``-p`` mode those grants come from
``stdin`` (which we close) or from ``--dangerously-skip-permissions``
(which auto-approves). Adding the flag by default would silently
broaden tool access for every test run — a security trade-off we
shouldn't make on behalf of the user. The hint tells them where to
look if they hit the timeout and want to investigate further.

> **Insight.** **Subprocess wrappers need three orthogonal defenses
> against indefinite hangs: bounded I/O (close write-channel after
> last write), bounded time (timeout + cancel path), and bounded
> visibility (tick timer + diagnostic preface).** Any one of them
> alone leaves a class of hangs unaddressed. Bounded I/O fixes
> child processes waiting on stdin you'll never send; bounded time
> fixes slow-but-progressing tasks that the user gives up on; and
> bounded visibility fixes the user thinking "is anything even
> happening?" while the child does in fact eventually return.
> The three are cheap to add and complementary; pick all of them
> when wrapping a CLI.

### 7.40 Abandon QProcess for the Test Skill runner — switch to subprocess+thread

**Symptom continuing from §7.39.** Even after path resolution
(§7.38), stdin-close (§7.39), and timeout (§7.39), Test Skill
*still* hung at "Starting…" for prompts as trivial as ``hello``.
Same machine, same ``claude.EXE`` binary, **Check Claude Step 2
worked fine** — but Test Skill's QProcess invocation produced no
response.

**The pivot.** The user sent a minimal working PySide6 playground
that uses ``subprocess.run(["claude", "--print", prompt],
capture_output=True, text=True, encoding="utf-8")`` and reliably
gets a response on the same machine. That's a decisive data point:
``claude`` works fine when invoked via ``subprocess``, on the same
PATH, in the same process. The bug isn't in ``claude``, the
environment, or the path resolution — **it's in our QProcess
wrapping itself.**

**Hypothesis on the QProcess failure mode.** ``subprocess.Popen``
with ``stdin=subprocess.DEVNULL`` makes the child's stdin file
descriptor point at the OS null device from the moment of spawn.
Any read returns EOF immediately. ``QProcess``'s
``closeWriteChannel`` is different: it closes our end of a *pipe*
that the child may already have done a blocking ``ReadFile`` on.
On Windows in particular, a child blocked inside ``ReadFile`` on
a pipe handle doesn't always unblock cleanly when the write end
closes — the read can persist until the OS recognizes EOF on the
handle, which has been observed to take effectively forever in
some pipe-state configurations. This is consistent with our
symptom: child spawned, child blocked on stdin read,
``readyReadStandardOutput`` never fires, ``finished`` never fires,
status stays at "Starting…". Check Claude Step 2 may have worked
by luck — ``"Reply with OK"`` is trivial enough that ``claude``
may emit its response before ever attempting the stdin read.

We don't need to definitively prove the hypothesis to fix it. The
right move is to **stop fighting QProcess and use what's proven
to work in the user's environment**: ``subprocess.Popen``.

**Implementation.** ``TestSkillDialog``'s runner is now built on
``subprocess.Popen`` in a Python ``threading.Thread``, communicating
back to the GUI thread via two Qt signals:

* ``_worker_result(exit_code, stdout, stderr)`` — process
  completed normally, with non-zero exit, or because the GUI killed
  it from Cancel / Timeout.
* ``_worker_failed(error_message)`` — Popen itself raised
  (FileNotFoundError, PermissionError, …) or ``communicate()``
  raised an unexpected exception.

The worker thread runs ``_worker_main``:

```python
proc = subprocess.Popen(
    cmd,                              # full path + ["-p", prompt]
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    stdin=subprocess.DEVNULL,         # proven-working from playground
    cwd=cwd,
    encoding="utf-8",
    errors="replace",
)
with self._worker_lock:
    self._subproc = proc
stdout, stderr = proc.communicate()
self._worker_result.emit(proc.returncode, stdout, stderr)
```

**Why this is safe across threads.** Qt signals are thread-safe.
When ``emit()`` is called from a non-GUI thread, Qt automatically
queues the signal through the GUI thread's event loop
(``Qt::QueuedConnection``) — so the result handler always runs in
the GUI thread where widget updates are legal. No explicit thread
marshalling needed.

**Cancellation / timeout via shared subprocess handle.** The
``_worker_lock`` guards the ``_subproc`` attribute against the
worker-vs-GUI race during construction. Once the worker has
assigned it, the GUI thread can read it under the lock and call
``proc.kill()`` to terminate. ``communicate()`` in the worker
returns shortly after, the worker emits its result, the GUI
classifies the verdict using the ``_was_cancelled`` /
``_timed_out`` flags set just before the kill.

**Tick timer no longer driven by subprocess I/O.** Previously the
tick checked ``self._process is None`` (a QProcess reference);
now it checks ``self._worker_thread is None`` (the Python thread).
Since ``subprocess.communicate()`` blocks the worker until the
child exits, **all output arrives at once at the end** — the
status label just reports elapsed time during the wait, without
any per-chunk byte count. This is a small UX regression from the
QProcess streaming model, but the QProcess streaming wasn't
working anyway, so trading hypothetical streaming for actual
reliability is the right call.

**`CheckClaudeDialog` left on QProcess.** It works for the user
(both steps PASSED), and the symptom suggests the QProcess bug
is sensitive to the specific timing / stdin-read pattern that
``claude -p`` exhibits for slower prompts. The two-step health
check uses ``claude --version`` (returns instantly, no stdin
read) and a trivial prompt (probably returns before any stdin
issue manifests). We could migrate it to ``subprocess`` for
consistency, but that's risk for no demonstrated gain. Left for
a future iteration if the bug surfaces there too.

**`try/except` around `_on_run`.** Mirrors the §7.37 defense in
``CheckClaudeDialog``. PySide6 silently swallows exceptions from
queued slot calls; without the wrapper, any synchronous failure
in the runner setup (Popen kwarg error, Thread construction
issue) would land as the §7.37 "button disabled + status frozen"
state with no visible reason. The wrapper writes
``traceback.format_exc()`` into the response pane so any failure
mode is fixable.

> **Insight.** **When wrapping the same external tool with two
> different APIs gives different results, prefer the one that
> works.** This sounds obvious, but in practice teams often
> double down on the broken wrapper ("we should be able to make
> QProcess work — there must be a flag or signal we're
> missing"). After three iterations of QProcess hardening
> (timeout, stdin-close, path resolution) that didn't budge the
> symptom, switching wrappers was the right call. The cost is
> low — ``subprocess.Popen`` + ``communicate()`` in a daemon
> thread is ~30 lines — and the benefit is reliability. The
> philosophical lesson: **wrapper choice is a design decision,
> not a religion.** If the same external dependency works through
> wrapper A and not through wrapper B, that's evidence about
> wrapper B's interaction with the dependency, not evidence that
> the dependency is broken.

### 7.41 Match the playground exactly — synchronous + heavy logging

**Symptom.** After §7.40 (switching ``TestSkillDialog`` from
QProcess to ``subprocess.Popen`` in a daemon thread), the dialog
*still* hung at "Starting…" for the simplest possible prompt
(``hello``). The user pointed out that their minimal PySide6
playground using plain ``subprocess.run(["claude", "--print",
prompt], capture_output=True, text=True, encoding="utf-8")``
worked reliably on the same machine. **The bug must be in our
wrapping**, not in subprocess itself or in ``claude``.

**Diagnostic move: collapse the diff to zero.** Match the
playground's invocation *exactly* and add comprehensive logging
so any deviation is visible. The customizations we'd accreted
on top of the basic call:

| Our code | Playground | Effect of difference |
|----------|------------|----------------------|
| Full resolved path via ``find_claude_executable`` | Bare ``"claude"`` | Different ``CreateProcessW`` lookup |
| ``-p`` short flag | ``--print`` long flag | Equivalent per ``claude --help``, but worth ruling out |
| ``stdin=subprocess.DEVNULL`` | (default = inherit parent's) | Different file-descriptor semantics |
| ``cwd=str(Path.home())`` | (default = inherit parent's) | ``claude`` config / project-skill resolution may behave differently |
| ``subprocess.Popen`` in worker thread | ``subprocess.run`` in GUI thread | Thread context, GIL semantics, signal-marshalling differences |
| Manual ``stdout/stderr=PIPE`` kwargs | ``capture_output=True`` shorthand | Functionally identical, but the shorthand is what they wrote |

Any one of these could be the silent killer. Instead of bisecting,
**collapse to the playground baseline first**: prove the dialog
works at all, then re-introduce customizations one by one to find
the culprit.

**Implementation.** ``TestSkillDialog._on_run`` now uses:

```python
cmd = ["claude", "--print", prompt]
result = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=timeout_s,
)
```

Run **synchronously in the GUI thread**, exactly like the
playground. The button is disabled before the call and the
status label reads ``"Running (UI frozen during call)…"`` — the
user knows the UI is intentionally unresponsive. A single
``QApplication.processEvents()`` call before the blocking
``subprocess.run`` paints the diagnostic preface to the screen
so the user can read it during the wait.

All worker-thread state (the ``_subproc`` field, ``_worker_thread``,
``_worker_lock``, ``_worker_result`` / ``_worker_failed`` signals,
``_worker_main`` / ``_on_worker_result`` / ``_on_worker_failed`` /
``_kill_subproc`` methods) is **removed** from ``TestSkillDialog``.
~140 lines of code gone. ``_on_cancel`` and ``_on_timeout`` become
no-op stubs (no async loop to deliver them into); ``_on_tick`` is
also a no-op stub (no event loop running during the blocking
call). All four are kept as wired-up stubs in case a future
iteration re-introduces an async runner — the connection points
are preserved.

**Heavy logging at every step.** A module-level ``_log(*args)``
prints to ``sys.stderr`` with a stable ``[test_dialog]`` prefix
and ``flush=True``:

```
[test_dialog] ============================================================
[test_dialog] _on_run START
[test_dialog]   prompt: 'hello'
[test_dialog]   cmd: ['claude', '--print', 'hello']
[test_dialog]   timeout: 180s
[test_dialog]   skill: skill-search (Global)
[test_dialog]   Python: 3.13.x (CPython, …)
[test_dialog]   platform: win32
[test_dialog] about to call subprocess.run(...)
[test_dialog] subprocess.run RETURNED in 4.3s
[test_dialog]   exit_code: 0
[test_dialog]   stdout: 1247 chars
[test_dialog]   stderr: 0 chars
[test_dialog] _on_run END
```

Visible whenever the app is launched from a terminal (``python
main.py`` in PowerShell). For users running from a desktop
shortcut where stderr isn't captured, the same breadcrumbs are
also echoed into the response pane via ``_append_response`` —
nothing important happens silently.

**`try/except` everywhere.** Outer wrapper around ``_on_run`` with
``traceback.format_exc()`` written into the response pane on any
internal failure — exception is also logged to stderr. Inner
``try/except/except/except`` around the ``subprocess.run`` call
itself with three specific branches: ``TimeoutExpired`` (renders
troubleshooting hints), ``FileNotFoundError`` (renders the §7.38
PATH diagnostic), generic ``Exception`` (renders traceback). No
failure mode falls into "silent half-toggled state."

**Why give up on async for now.** Three iterations of
async-runner hardening (QProcess, then QProcess + stdin-close +
timeout, then subprocess + thread) didn't fix it on this user's
machine. The synchronous runner trades UI responsiveness during
the call (~5-30s of frozen UI) for the certainty that the
*correct* call shape executes. Once we know it works, re-adding
async is incremental work; trying to debug async without first
confirming the synchronous baseline works was upside-down.

**Re-introduction path (deferred).** After the synchronous path
is verified working, future iterations can re-add non-blocking
behavior by either: (a) Popen + a poll loop that calls
``QApplication.processEvents()`` between ``poll()`` checks, or
(b) the threading approach but with the playground-matching
invocation shape (bare ``"claude"``, inherited stdin, inherited
cwd, ``capture_output=True``). The diagnostic value of having
*either* approach work is high — we can incrementally walk the
table back and find which customization broke threading.

> **Insight.** **When a working reference exists and your code
> doesn't work, collapse the diff before debugging.** The
> playground was a tiny, working, complete reference — the
> fastest path forward wasn't to enumerate what could be wrong
> with our code, it was to match the working code line-by-line
> and then add our customizations back one at a time. This
> "binary-search the diff" technique is faster than reasoning
> about the broken state and works regardless of which side
> contains the bug. Once you have a working baseline you can
> iterate confidently; without one, every change is just adding
> another variable.

### 7.42 ROOT CAUSE: `cursor.End` doesn't exist on PySide6 6.5+

**The bug, finally surfaced.** After §7.41 added comprehensive
stderr logging, the user's terminal output revealed the actual
exception that had been hiding behind every "hang" symptom from
§7.34 through §7.41:

```
[test_dialog] OUTER EXCEPTION in _on_run:
Traceback (most recent call last):
  File "...test_dialog.py", line 518, in _on_run
    self._append_response("\n".join(diag_lines))
  File "...test_dialog.py", line 654, in _append_response
    cursor.movePosition(cursor.End)
AttributeError: 'PySide6.QtGui.QTextCursor' object has no attribute 'End'
```

**PySide6 6.5+ enforces strict enum scoping** by default. The
legacy shorthand ``cursor.End`` — which worked in PyQt5 and older
PySide builds via implicit enum forwarding — **raises
AttributeError** on this PySide6. The strict form is
``QTextCursor.MoveOperation.End``. From PySide6's 6.5 release
notes (paraphrased): "Enum values are no longer accessible as
instance attributes of the containing class; use the fully
qualified enum reference."

**Every "hang" report from §7.34 onward was actually this bug.**
The pattern was always the same:

1. ``_on_run`` (or QProcess's ``readyRead`` slot) tries to write
   into the output pane via ``_append_response`` / ``_append_text``.
2. ``cursor.movePosition(cursor.End)`` raises ``AttributeError``.
3. The exception either escapes (TestSkillDialog: outer ``try/except``
   tried to write the traceback into the pane, which raised the
   SAME error) or is silently swallowed (CheckClaudeDialog: I had
   wrapped ``_append_text`` in ``try/except: pass`` "defensively",
   which hid the bug).
4. UI ends up in a half-toggled state — buttons disabled, status
   stuck at "Starting…" / "Running…", output pane empty.
5. User reasonably concludes the subprocess is hanging.

**The CheckClaudeDialog "PASSED" verdicts were actively
misleading.** The status label is updated via ``QLabel.setText``,
which doesn't touch a QTextCursor — that worked fine and showed
``DONE in 0.1s · 25 bytes received``. But the output pane
operations (which would have shown the actual ``claude
--version`` text) all silently failed. The verdict text was right;
the rendered response was missing. The user reasonably reported
"passed and got responses" because the status said DONE.

**Why six iterations of fixes didn't catch this.** Every fix
attacked a different plausible cause:
* §7.35 added the tick timer (assumed buffered stdout).
* §7.36 added the health check dialog (assumed CLI install).
* §7.37 added try/except wrapping (assumed silent slot
  exception — close, but missed the actual exception class).
* §7.38 added path resolution (assumed wrong exe name).
* §7.39 added stdin-close + timeout (assumed pipe deadlock).
* §7.40 switched to subprocess+thread (assumed QProcess wrapper
  bug).
* §7.41 went fully synchronous + stderr logging (matched
  playground).

Of these, §7.37 was the closest — but it caught the exception
and tried to write the traceback into the *same* widget that was
raising the exception, so the traceback never appeared. The
defensive ``try/except: pass`` in CheckClaudeDialog's
``_append_text`` was actively harmful — without it, the
AttributeError would have surfaced as a Python traceback in the
console as early as §7.36 and saved five iterations.

§7.41's heavy stderr logging was what finally exposed it: the
exception was now printed *before* attempting any widget write,
so the user could see the actual error message in their terminal.

**Fix.** Two-line change:

```python
# Before:
cursor.movePosition(cursor.End)

# After:
from PySide6.QtGui import QTextCursor
cursor.movePosition(QTextCursor.MoveOperation.End)
```

Applied to both ``test_dialog.py:_append_response`` and
``check_claude_dialog.py:_append_output``. The defensive
``try/except: pass`` wrapper around CheckClaudeDialog's
``_append_output`` is **removed** — it was the cover that let
the bug go undiagnosed for six iterations.

**Verification:**

```python
$ python -c "from PySide6.QtGui import QTextCursor;
              print(QTextCursor.MoveOperation.End)"
MoveOperation.End
```

The enum resolves; the modules import; the user should now see
actual responses in both dialogs.

**Lessons for future iterations:**

1. **Never wrap widget update calls in ``try/except: pass``.** A
   failed widget update is a bug, not a hardware fault. Silent
   ``pass`` defeats the purpose of having exceptions in the first
   place. If you genuinely need to handle it, log the exception
   visibly — at minimum to stderr.

2. **Strict enum scoping is the PySide6 6.5+ default.** Pin all
   enum references to the fully qualified form (``Class.EnumName.Value``)
   rather than relying on the instance-attribute shorthand.
   Audit existing code for ``object.SOME_ENUM_VALUE`` patterns
   that pre-dated this change.

3. **Mirror diagnostics to stderr early, not late.** Six iterations
   of UI-side debugging missed the actual exception because we
   were trying to render the diagnostic into the same widget that
   was broken. The §7.41 ``_log()`` helper printing to stderr was
   what unblocked us. Reach for stderr (and ideally a real
   logging framework) as the *first* line of diagnostic surface,
   not as a fallback after the UI surface fails.

4. **An exception's call site matters more than its message.**
   The user's stderr trace pointed at the EXACT line
   (``cursor.movePosition(cursor.End)``) and the EXACT type
   (``AttributeError``). Once we had that, the fix was a
   five-second change. Every prior iteration was working from
   the user's symptom description ("it hangs") without ever
   seeing the actual Python exception — which is why the bug
   eluded us. **Bug-from-symptoms is intrinsically slower than
   bug-from-traceback;** when you have a working environment
   that can produce a traceback, get the traceback before
   theorizing.

> **Insight.** **A `try/except: pass` is a debt instrument.**
> It "fixes" a problem in the moment by hiding the exception,
> but the bug accumulates interest — every subsequent debugging
> session has to fight through the silenced failure. The cost of
> the silence in this iteration was six rounds of misdirected
> hardening (~1500 lines of defensive code, four new design-doc
> sections, multiple architectural pivots) before the right line
> got printed to a terminal. **Whenever you write `except ...:
> pass`, write a comment explaining what you're hiding and why —
> and if the answer is "I don't know but I want it to not
> crash", don't write the wrapper. Let the exception propagate.**
> A crash with a traceback is a diagnostic gift; a silent no-op
> is a debugging tax with no end date.

### 7.43 Re-introduce the async runner — now that the real bug is fixed

**Context.** §7.41 abandoned the threaded runner and switched to
synchronous ``subprocess.run`` in the GUI thread as a diagnostic
move — the playground-matching baseline was the fastest path to
*any* working invocation. §7.42 then found the real bug
(``QTextCursor.End`` AttributeError) which had been clobbering
every output update across every prior async attempt.

With the real bug fixed, the **synchronous baseline works**, but
freezes the GUI for the call duration (5-30s typical, sometimes
60s+ for skill-routing prompts). The user shouldn't have to stare
at a frozen window — and the async pattern was always the right
design, we just had a different bug masking it.

**Implementation.** Threaded subprocess + Qt-signal-marshalled
result delivery, mirroring the §7.40 design but with the
playground-matching ``Popen`` shape from §7.41:

```python
proc = subprocess.Popen(
    cmd,                          # ["claude", "--print", prompt]
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    errors="replace",
    # No stdin override → inherit parent's (matches playground).
    # No cwd override → inherit parent's (matches playground).
)
stdout, stderr = proc.communicate()
self._worker_result.emit(proc.returncode, stdout, stderr)
```

Two Qt signals route worker → GUI:

* ``_worker_result(exit_code, stdout, stderr)`` — process
  completed (normally, with non-zero exit, or because we killed
  it from Cancel/Timeout).
* ``_worker_failed(error_message)`` — ``Popen`` or
  ``communicate()`` raised. The handler renders the §7.38 PATH
  diagnostic for not-found-style failures.

Qt's signal system automatically routes cross-thread emissions
via ``Qt::QueuedConnection`` — slot bodies always run on the GUI
thread where widget mutations are legal.

**Cancellation + timeout.** ``_worker_lock`` guards the
``_subproc`` handle for safe cross-thread access. The GUI thread
reads under the lock and calls ``proc.kill()`` from ``_on_cancel``
or ``_on_timeout``; the worker's ``communicate()`` returns
immediately after, the worker emits its result, the handler
labels the verdict using the ``_was_cancelled`` / ``_timed_out``
flags set before the kill.

``_is_running()`` is the single source of truth for "is a run in
progress" — used by ``_on_run`` to reject re-entry, ``_on_tick``
to gate elapsed-time updates, ``_on_timeout`` / ``_on_cancel`` to
skip stale firings.

**Tick timer fires every 500ms** because the GUI thread is free
(the work is on a background thread). Status reads
``Running… 12.4s · waiting for response (timeout in 168s)`` —
elapsed time plus countdown to the hard 3-minute backstop.

**Diagnostic preface unchanged from §7.42** (clean
playground-matching command display, no internal references).
Heavy stderr logging from §7.41 remains, with new tagged origins:
``[worker]`` prefix for worker-thread breadcrumbs, ``[gui]``
prefix for GUI-thread events. The ``=`` separators between runs
let users skim a session log and find specific runs.

**Why this works now when §7.40 didn't.** §7.40 had the
**identical** runner architecture — Popen + Thread + Qt signals
— but every output-pane update was failing silently with the
``cursor.End`` AttributeError. The worker thread was running,
``communicate()`` was returning, ``_worker_result`` was emitting,
the slot was even running — but ``_append_response`` raised on
the very first line, the outer ``try/except`` tried to write the
traceback into the *same* widget (raised again), and the dialog
stayed visually empty. The §7.42 fix to use
``QTextCursor.MoveOperation.End`` removes the AttributeError, and
the architecture works exactly as designed.

**Lesson archived.** Whenever the symptom of a "wrapper choice"
hang is "no output ever appears," check the *output mechanism*
first, not the wrapper. If text isn't getting onto the screen,
the failure is between the slot and the QPlainTextEdit, not
between the subprocess and the slot. Adding stderr logging
(§7.41) was what surfaced this — before that, every diagnostic
went into the same broken widget, so each "fix" looked equally
broken.

> **Insight.** **A failed iteration's design isn't necessarily
> wrong — it just may have been masked by an unrelated bug.**
> The threaded runner in §7.40 looked like a "doesn't work" data
> point and we backed off to synchronous in §7.41. With hindsight
> the threaded runner was always correct; the visible symptom
> just happened to also hit the same widget that the cursor.End
> bug clobbered. Architectural decisions should be re-validated
> after fixing unrelated bugs that affected the previous
> evaluation — old "didn't work" verdicts can flip to "works
> fine" once the actual blocker is gone.

### 7.44 Split response view: rendered Response + chronological Raw Output

**Spec.** Once the runner started returning real responses (§7.43),
the dialog's single output pane was doing two jobs at once — it
showed both the *diagnostic preface + raw text* (useful for
debugging) and *the actual model response* (which is what the user
came here to see). A claude response with markdown headers,
bulleted lists, code fences, and ★ Insight boxes rendered as raw
text alongside ``$ claude --print …`` / ``[done in 69.3s …]``
chrome was visually noisy and hard to read.

**Fix.** Replace the single ``QPlainTextEdit`` with a nested
``QTabWidget`` containing:

* **Response** (default tab) — a ``QTextBrowser`` rendering the
  model's stdout via ``setMarkdown``. Headers, lists, fenced code,
  blockquotes, and the inline ★ Insight separators all render
  cleanly. Empty/whitespace stdout shows a faded italic "no
  response — see Raw Output" placeholder so the tab is never
  ambiguously blank.
* **Raw Output** — the previous ``QPlainTextEdit`` behavior:
  diagnostic preface (``$ claude --print 'hello' / (timeout: 180s
  …)``), raw stdout, stderr lines, and the verdict marker
  (``[done in X.Xs · N chars total]`` / ``[cancelled]`` / ``[timed
  out]`` / ``[error]``). The chronological monospace dump for
  anyone debugging the run.

**No auto-tab-switching.** The user stays on whichever tab they
care about. Response is the default because the common case is "I
want the answer"; Raw Output is one click away for "I want to
debug why the answer looks wrong."

**Method-level changes:**

* ``_append_response`` renamed to ``_append_raw`` to make its role
  explicit — it writes to Raw Output, not the rendered Response
  pane.
* New ``_set_response_markdown(text)`` — renders ``text`` into the
  Response tab via ``QTextBrowser.setMarkdown``; falls back to an
  italic faded placeholder for empty input.
* New ``_clear_run_views()`` — clears both panes; called from
  ``_on_run`` start and from Clear.

**Verdict-specific Response rendering.** Each terminal state
writes an appropriate Response payload:

| Verdict | Raw Output | Response |
|---------|------------|----------|
| Success | stdout + `[done in Xs · N chars]` | stdout rendered as markdown |
| Non-zero exit | stdout + `[exit N]` | stdout rendered (some CLIs emit useful messages on non-zero exit) |
| Cancelled | `[cancelled by user]` | italic "cancelled by user after Xs" |
| Timed out | `[timed out]` + troubleshooting hints | bold "Timed out" + pointer to Raw Output |
| FailedToStart | full `claude_path_diagnostic` | bold "Could not launch claude" + pointer to Raw Output + Check Claude |
| Worker exception | traceback | bold "Worker error" + first-line summary + pointer to Raw Output |

Each Response payload tells the user *what happened* and points at
the Raw Output tab when there are details to dig into — so a
quick-glance user can read just Response, while a debugging user
naturally migrates to Raw Output.

**Why `setMarkdown` is sufficient.** Qt's built-in CommonMark
parser (used here and in the Skill Description tab from §7.34)
handles every formatting feature claude actually emits — headers,
emphasis, lists, fenced code with language tags, blockquotes,
horizontal rules, links, inline code. We considered adding a
proper markdown library (``mistune`` or ``markdown-it-py``) but
the in-tree parser is good enough and stays consistent with the
rest of the app. If a future claude response uses GFM tables or
some other CommonMark-extension feature that doesn't render
correctly, that'd be a reason to revisit.

> **Insight.** **One view, one job.** A pane that's trying to be
> both "the answer" and "the debug log" optimizes for neither —
> diagnostic chrome makes the answer harder to read, and the
> answer's natural markup makes the chrome harder to scan. The
> usual fix is two panes/tabs with explicit roles, defaulting to
> the one the user came for. The Raw Output tab is the "I want
> to see what really happened" escape hatch — preserved because
> sometimes you DO need it, but tucked out of the way so it
> doesn't clutter the common case.

### 7.45 API-key Settings field silently broke working `/login` auth

**Symptom.** After adding Help → Settings → API key (§ Test Skill
wiring), a user with a perfectly working `claude /login`
subscription typed *something* into the API key field, hit Run, and
got back a 39-char stdout:

```
Invalid API key · Fix external API key
```

The log file confirmed the wiring did its job:

```
[worker] env_overrides keys: ['ANTHROPIC_API_KEY']
[worker] communicate returned: exit=1
  stdout: 39 chars
  stderr: 0 chars
```

i.e. the app sent `ANTHROPIC_API_KEY=<whatever-user-typed>` and
`claude` rejected the value.

**Cause.** Claude Code has **two mutually exclusive auth paths**:

| Auth mode             | Trigger                          | Token type                |
|-----------------------|----------------------------------|---------------------------|
| Subscription login    | `claude /login`                  | Account session           |
| Pay-as-you-go API key | `ANTHROPIC_API_KEY` in env       | `sk-ant-…` from console   |

When `ANTHROPIC_API_KEY` is set in the child env, Claude Code uses
it and **bypasses** the user's `/login` auth. So a Pro/Max user who
filled the field with anything other than a valid console key just
broke their own working setup.

The wiring helper (`claude_env_overrides`) treats "non-empty
string" as "user wants this key" — which is mechanically correct
but obscures that toggling from empty to any-value changes the
auth path entirely. The Settings dialog accepted free-text input
with only a tooltip explaining the trade-off; tooltips don't fire
on first paint, so the trap was easy to walk into.

**Fix (in `ui/settings_dialog.py`).**

1. **In-place warning under the field**, surfacing the
   `/login`-vs-API-key trade-off at input time:

   > *If `claude` already works in your terminal (via `/login`
   > subscription), leave this **empty**. Setting any value here
   > overrides that auth and must be a valid `sk-ant-…` key from
   > `console.anthropic.com`.*

2. **Whitespace strip on save** — a trailing newline pasted from
   email or a doc would silently break an otherwise-valid key, with
   the failure showing up only as the opaque "Invalid API key" at
   the next test run. `text().strip()` before `set_api_key`.

The wiring itself is correct and unchanged — the fix is entirely
in how the dialog presents the choice to the user.

> **Insight 1.** When **absence is load-bearing in the downstream
> system**, the input UI needs to make that absence-state visible —
> not just allow the user to "fill it in." Our helper's
> "set-if-non-empty" semantics are mechanically right, but they hide
> the side effect of crossing the empty boundary. Tooltips don't
> count: they fire on hover, not on first paint, so users discover
> the trap only after stepping into it. The cure is a hint label
> in normal flow, costing one row of dialog real estate.
>
> **Insight 2.** Logging `yes/no` for the API-key override
> presence (instead of the actual value) was the right *security*
> choice — never paint secrets into a log file the user might
> attach to a bug report. But it made this exact diagnosis harder
> because the log says "key sent" without saying *which* key. The
> right resolution isn't to log secrets; it's to make the
> **input itself trustable**: whitespace strip + reveal toggle
> (§ eye icon) + warning text. The user verifies the key by
> reading the field, not by reading the log.
>
> **Insight 3.** This is the second time the
> "settings persisted, not yet read at run time" gap has bitten
> (see §7.x for the dirty-flag refactor's predecessor). Persisting
> a value the system doesn't yet honor is a near-invisible
> regression — the UI works, the storage works, but the
> end-to-end effect is missing. The lesson: when adding a
> persisted preference, wire the **read** alongside the **write**
> in the same change set, or write a `# TODO: not yet read` comment
> on the getter so a later reader knows it's a half-installed
> contract.

---

### 7.46 Multi-turn context in the Test Skill dialog

**Symptom / user request.** Each click of **Run** in a Test Skill
window spawned a fresh `claude --print …` subprocess, so the second
prompt had no idea what "this skill," "the previous answer," or "now
expand on point 2" referred to. The user observed: *"With an opened
'Test Skill' window, user may run multiple calls (one after another
prompt), will all prompts are in the same session? Did Claude
remember the previous context?"* — and confirmed they wanted context
preserved across runs in the same window.

**Diagnosis.** `claude --print <prompt>` is **stateless by design**:
read prompt, emit response, exit. No session continuation unless
`--resume <session-id>` or `--continue` is passed. The dialog was
spawning a brand-new `subprocess.Popen` per click with no
session-tracking flags, so every run was an independent one-shot
conversation as far as `claude` was concerned. This is the right
default for scripted/CI use — and was a deliberate choice when the
dialog was first built — but for a human iteratively probing a
skill, the lack of continuity felt broken.

**Decision: opt-in resume with JSON output as the carrier.**
The simplest change that doesn't disturb existing behavior:

1. Add a `Continue conversation` checkbox in the prompt header row.
   **Checked by default** — the user's explicit preference once the
   feature shipped: *"This 'Continue conversation' checkbox should
   be checked by default."* The reasoning: a Test Skill window is
   the natural place for iterative follow-up prompts ("now expand
   on point 2", "what about X"), and discovering after Run #2 that
   nothing was remembered is a worse outcome than the small
   per-run cost of a JSON envelope parse on a one-shot. Users who
   genuinely want one-shot semantics can untick.
2. When checked, the next Run does two things simultaneously:
   * If a session id is already captured, append `--resume <id>`
     so `claude` restores the prior turns into context.
   * Append `--output-format json` so `claude` emits a structured
     envelope instead of bare markdown. We parse the envelope's
     `session_id` field and stash it on the dialog for the next
     turn.
3. The `Clear` button is the only **forget** gesture. Toggling the
   checkbox off-then-on does NOT drop the session — that would
   surprise users who flip the box to compare a fresh-prompt run
   against the continuing context.

**Why JSON only when continuing.** `--output-format json` *always*
would work, but adds a parsing failure mode (malformed envelope →
fallback to raw stdout) for zero benefit when the user isn't
asking to continue. The conditional keeps the no-feature path
(checkbox unticked) byte-for-byte identical to the pre-change
invocation — important because §7.41 spent a full debug session
pinning down the playground-matching invocation shape, and
gratuitously diverging from it invites regressions. The default
*is* checked, but the escape hatch is one click away.

**Why the session id survives toggling.** The mental model is "the
checkbox controls whether *this Run* continues," not "whether the
session exists at all." A user who turns Continue off to ask one
side-question, then turns it back on, expects the original thread
to resume — not to be forced to retype everything to recover state
they had a moment ago. The session id is per-dialog-lifetime: it
dies when the user clicks Clear or closes the dialog.

**Resume id resolution rules.** Three states:

| Checkbox | `_session_id`      | Effect                                       |
| -------- | ------------------ | -------------------------------------------- |
| off      | any                | Plain text invocation. No `--resume`, no JSON. |
| on       | None (first turn)  | JSON output only. Captures a new session id. |
| on       | set                | `--resume <id>` + JSON output. Continues + refreshes id. |

The third row matters: `claude` returns a (possibly new) session id
each turn — we always overwrite from the envelope rather than
treating the id as immutable, so internal session-id rotation can't
strand the dialog.

**Implementation surface.**

* `skill_introspect.build_claude_command` gains two kwargs:
  `session_id: str = ""` (appended as `--resume <id>` when
  non-empty) and `json_output: bool = False` (appended as
  `--output-format json`). Both default to the pre-existing
  shape so no caller needs to change.
* `skill_introspect.parse_claude_json_envelope(stdout)` returns
  `(response_text, session_id_or_None, is_error)` — pure Qt-free
  helper, falls back to raw stdout on parse failure so the user
  always sees *something*.
* `TestSkillDialog._session_id: str | None` — captured on every
  continue-mode successful run; surfaced in a small faded
  `(session: abc1234…)` indicator next to the checkbox so the
  user has visual confirmation continuity is active.
* `_last_run_was_continue: bool` — snapshotted in `_on_run` so
  `_on_worker_result` knows whether to parse JSON (instead of
  re-reading the checkbox, which the user might have toggled
  during the run).

**Edge cases handled.**

* **Malformed JSON.** `json.loads` raises → return raw stdout as
  the response, no session id captured. The user sees the
  unparsed envelope in the Response tab and a non-fatal `no
  session_id in JSON envelope; next continue Run will start
  fresh` line in the log.
* **`is_error: true` in envelope.** Prefix the rendered output
  with `**Claude reported an error for this turn.**` so a
  rate-limit / permission / tool-denial response isn't read as a
  normal answer.
* **Expired session id.** `claude --resume <stale-id>` errors out;
  it surfaces normally through `_on_worker_result`'s non-zero-exit
  branch. User clicks Clear to forget the stale id and starts
  fresh. We deliberately don't preemptively validate session ids —
  that would require an extra round-trip per Run for a vanishingly
  rare case.

**Insights worth keeping.**

> Two flags, one toggle. `--resume <id>` and `--output-format json`
> *seem* orthogonal (resume is about input, JSON is about output)
> but the feature couples them: without JSON we can't see the new
> session id, and without resume we can't actually use it. Tying
> them to one checkbox keeps the user from having to understand
> the coupling; bundling them at the API level
> (`build_claude_command(..., session_id="...", json_output=True)`)
> documents it for the next reader.

> The default branch must be byte-identical to the previous
> behavior. Three prior sections (§7.40-§7.42) chased the most
> expensive bugs in this project because we diverged from the
> known-working invocation shape. The new code path is gated by
> `continue_mode = self.continue_checkbox.isChecked()` so a user
> who never ticks the box runs the exact argv list, exact env,
> exact parse pipeline that shipped before. New features that
> *might* break old paths shouldn't, and the cheapest way to
> guarantee that is to leave the old path untouched.

> Forget is a user gesture, not an automatic consequence. Several
> alternative designs auto-cleared the session id on Cancel /
> Timeout / non-zero exit. They felt clean from the implementor's
> seat but were wrong from the user's: a cancelled or timed-out
> turn doesn't invalidate the *previous* turns, only the current
> one. Bind "forget" to a single, explicit, user-visible button
> (Clear) — anything else creates ghost-state surprises where the
> next Run silently doesn't continue and the user can't say why.

---

### 7.47 Two persistence layers: session id vs. Claude Code project memory

**Symptom / user observation.** Shortly after §7.46 shipped, the
user (Wei / Smith) reported: *"With the first Test Skill window, I
told Claude my name is 'Smith', then close the window. Next I
select another skill to open another Test Skill window, I type
prompt 'What is my name?', the Claude returns 'Your name is Smith
(per the memory record updated 2026-05-12).' Is this behavior
normal? In my opinion, different windows should maintain its own
session, am I correct?"*

The mental model behind the question was sound — windows *should*
not share conversation state — but the observed cross-window
"remembering" looked like a leak from our `--resume` plumbing.

**Diagnosis.** Two completely separate persistence mechanisms were
both in play, and only one of them is something this app can or
should control:

1. **Layer 1 — our per-window session id (§7.46).** Each
   `TestSkillDialog` owns its own `_session_id`. Dialog destruction
   (via `WA_DeleteOnClose`) drops the id. A new dialog for any skill
   starts with `_session_id = None`, sends no `--resume <id>`, and
   `claude` allocates a fresh conversation. **No leakage between
   windows** — the user's instinct was correct.
2. **Layer 2 — Claude Code's own project memory.** Independent of
   any flag we pass, `claude` loads a project-scoped memory file at
   the start of every invocation:

       ~/.claude/projects/<project-slug>/memory/MEMORY.md

   When the user introduced themselves as "Smith" in window 1, the
   subprocess Claude decided that fact was worth persisting (per
   Claude Code's standing instructions about saving user facts) and
   wrote `user_name.md` plus an index entry to `MEMORY.md`. The
   write outlived the session that produced it. Window 2's brand-new
   session loaded that file at startup as part of its system context
   and answered from the saved record — *not* from any inherited
   conversation history.

The give-away in the response — *"per the memory record updated
2026-05-12"* — is Claude citing its memory layer directly.

**Decision: do nothing in the app, document the model.** Layer 2
is a deliberate Claude Code feature and exists at a layer our app
can't (and shouldn't try to) reach into:

* We invoke `claude` in its normal cwd (`working_directory_for()`
  returns the project root or `Path.home()`). Forcing a throwaway
  cwd to suppress memory loading would also break skill discovery,
  which is the whole point of shelling out to `claude` rather than
  the SDK.
* There's no clean per-invocation flag to disable memory loading
  in current `claude` CLI. Even if one existed, suppressing
  Layer 2 by default would mean *our* Test Skill window is a
  worse environment than the user's own terminal — defeating
  §7.34's "test against the same Claude Code the user actually
  runs" guarantee.
* The user's mental model isn't wrong; it's just incomplete. The
  fix is documentation, not code.

**Key file locations to know.** For this project on this user's
machine:

    ~/.claude/projects/C--work-claude-demo-Claude-Skills-Manager-GUI/memory/
    ├── MEMORY.md            (one-line index — auto-loaded on every claude invocation)
    ├── user_name.md         (the "name is Smith" record)
    └── feedback_*.md        (working preferences, e.g. continuity default)

Each memory file has YAML frontmatter (`name`, `description`,
`type`) and a markdown body. Some include `originSessionId` —
forensic only; the record is global to the project and outlives
the session that wrote it.

**The cwd-decides-which-memory rule (and how cwd is actually
chosen).** `claude` keys its memory directory off the cwd at
invocation time. **What cwd we hand it** has changed across this
project's history:

* **Pre-§7.48** (initial Test Skill implementation through §7.46):
  the `subprocess.Popen` call had no `cwd=` argument, so the
  subprocess inherited the Python process's cwd. Since our app
  never calls `os.chdir`, that's whatever directory the user
  launched the app from. **Every Test Skill run, regardless of
  skill type, used the same launch-time cwd.** This contradicts a
  defunct `working_directory_for(skill)` helper that *intended* to
  return per-skill-type directories (project root for Project
  skills, `~` for Global / Plugin) but was imported-without-call
  for the entire lifetime of the dialog. A vestigial import survived
  until §7.48 removed it.
* **§7.48 onward**: an explicit Working Directory control on the
  dialog. Defaults to `Path.cwd()` at construction (preserving
  the pre-§7.48 launch-dir behavior byte-for-byte), but the user
  can override per-window via a Browse button. The cwd is logged
  in the diagnostic preface every run, so the memory layer that
  will be loaded is always visible.

So two Test Skill windows opened from the *same* app launch share
the same default memory file unless one of them changes its
Working Directory. The original wording in this section
("different skill types load different memory") was based on the
helper's docstring, not on the code path — the helper was never
wired up. §7.48 documents the proper fix and the new user control.

**How a user can verify / defeat Layer 2.**

* **Verify isolation of Layer 1.** Open two windows. The session
  indicator next to each Continue-conversation checkbox shows a
  different short id — proof that the `--resume` channel is
  per-dialog.
* **Verify Layer 2 is doing the work.** Open
  `~/.claude/projects/<slug>/memory/MEMORY.md` in any editor. The
  cross-window "remembered" fact is visible verbatim. Deleting
  the record (or its referenced file) makes the next Run no
  longer know.
* **Soft-suppress Layer 2 for one turn.** Phrase the prompt with
  *"don't write this to memory"* — Claude usually honors that and
  keeps the fact only in the live session. Closing the window
  then loses it.
* **Hard-clear Layer 2.** Delete or edit the files under
  `…/memory/`. Effective at the next `claude` invocation.

**Insights worth keeping.**

> Two persistence mechanisms, one user surface. Layer 1 (per-window
> session) and Layer 2 (per-project memory) are orthogonal in
> implementation but **look identical from the user's seat** —
> both manifest as "Claude remembered something." When a user
> reports unexpected memory behavior, ask "is the recall coming
> from a session id or from a memory file?" before reaching for
> a fix. Diagnostic: check whether the `(session: …)` indicator is
> empty (Layer 2 must be the source) or populated (could be
> either; check the file on disk to disambiguate).

> Don't try to fence off features that live in the dependency.
> Layer 2 is Claude Code's, not ours. Suppressing it by changing
> the cwd, stripping `~/.claude/`, or shelling out with a sandbox
> harness would defeat §7.34's "use the user's real Claude Code"
> principle and create a Test Skill experience that doesn't match
> what the user would see in a terminal. The right call is to
> teach the user the model, not to lobotomize the subprocess.

> The `originSessionId` field in saved memory records is forensic,
> not binding. It tells you *which* session wrote the record but
> does **not** bind retrieval to that session. Future sessions
> still load it. This is why "I closed that window — why does
> the new window still know?" feels surprising: the closure
> destroyed the session, not the record the session created.

---

### 7.48 Per-window Working Directory control in the Test Skill dialog

**User request.** Following the §7.47 explanation of how cwd
determines which project memory `claude` loads, the user (Wei /
Smith) asked: *"How about keeping the default behavior — Test
Skill window runs Claude from the launch-dir. Is it possible to
add a setting 'Working Directory' control to Test Skill window
(like Claude Desktop app does)?"*

The ask reframes cwd from "a thing the app silently inherits"
into "a thing the user explicitly sets per window." Claude Desktop
uses this pattern: a per-conversation folder picker that scopes
file system, memory, and project settings to whatever directory
the user wants to experiment against.

**Diagnosis of the existing situation.** Two findings surfaced
while answering the cwd question that drove the design here:

1. The `_worker_main` in `ui/test_dialog.py` never passed
   `cwd=` to `subprocess.Popen`, so every Test Skill run inherited
   the launch directory. The §7.41 comment ("match the playground
   exactly … no cwd override") was a deliberate debug-era choice
   that made the inheritance implicit.
2. A helper `working_directory_for(skill) -> Path` had been written
   with a careful per-skill-type docstring (Project skills → project
   root; Global / Plugin → `Path.home()`), imported in
   `test_dialog.py`, and **never called**. Dead code that misled the
   §7.47 documentation. Removed in this iteration.

**Decision: dialog-scoped explicit control, launch-dir default.**

* The cwd is a property of the *dialog*, not the skill. Two windows
  for two skills can point at two unrelated directories. Closing
  and reopening a window resets to the launch dir — no QSettings
  persistence in v1. This matches Claude Desktop's per-conversation
  scoping and gives the user maximum flexibility without bolting on
  the question of "where does the saved-cwd preference live."
* Default value = `Path.cwd()` captured at dialog construction.
  This preserves the pre-§7.48 behavior byte-for-byte for users who
  don't touch the new control. `Popen(cwd=str(Path.cwd()))` is
  observably identical to `Popen(cwd=None)` for the no-override
  case, so the §7.41 playground-matching invariant remains intact.
* `cwd=` becomes an explicit kwarg to `Popen` (not omitted) — the
  diff between launch-dir-default and user-selected is a single
  string value, with all the surrounding logic identical. No
  "different code paths for default vs override" footgun.
* The user picks a directory via `QFileDialog.getExistingDirectory`,
  which guarantees the returned path exists at selection time.
  The picker is the *only* mutation path — the displayed
  `QLineEdit` is read-only — so a malformed pasted path can't make
  it into `_cwd`. If the directory disappears between selection and
  Run, Popen raises and the existing `_worker_failed` slot surfaces
  a clean error.
* Changing cwd does **not** clear the active session id. Per
  §7.47's "forget is a user gesture" principle, only the Clear
  button explicitly resets state — even if the new cwd's memory
  layer is logically incompatible with the current conversation.

**UI placement.** A dedicated row at the top of the Test tab,
above the existing prompt header. The visual order top-to-bottom
becomes:

    [ Working Directory: ┃ <path>                        ┃ [📁 Browse…] ]
    [ Prompt:                  (session: …) [✓ Continue] [✓ Prefix]    ]
    [ ……………… prompt edit ……………… ]
    [ Run ] [ Cancel ] [ Clear ]    status…    [busy]

The cwd row reads as "the setting that governs every Run in this
window," distinct from the per-prompt toggles next to "Prompt:".
A monospace font on the path display matches the rest of the
dialog's path-rendering convention (header strip, Raw Output pane).

**Diagnostic preface gets a `(cwd: <path>)` line — always.** Even
the default launch-dir case shows the cwd, because the answer to
"which memory file is `claude` going to load?" is non-obvious from
anywhere else in the UI. Showing it every run also makes
copy/paste reproduction in a terminal trivially correct.

**Insights worth keeping.**

> Dialog state vs. skill state — pick the lower-friction model.
> The cwd could be modeled as a skill property ("this skill always
> runs from project X") or a dialog property ("this window
> happens to be testing from project X"). The second is strictly
> more flexible: nothing prevents per-skill defaults being layered
> on top later via QSettings, but going the other direction —
> retrofitting per-window override onto per-skill cwd — would
> require breaking the skill's stable identity. Choose the more
> primitive scope; the richer one can always derive from it.

> "Default value equal to the previous implicit value" is the
> cheapest preservation guarantee. We didn't have to argue about
> whether wiring up `cwd=` would re-introduce §7.41-style
> regressions — `Popen(cwd=str(Path.cwd()))` is operationally
> identical to `Popen(cwd=None)` for the unchanged case. The new
> feature is purely additive; users who never touch the control
> see byte-identical behavior. Whenever you add a knob whose
> default value would otherwise be implicit, make sure the default
> path through the new code is observably the same as the path
> through the old code — it reduces the "does this regress
> anything?" review surface to zero.

> Read-only display + picker-as-only-mutation = robust without
> validation code. We deliberately chose a read-only `QLineEdit`
> for the cwd display so the only way to change `_cwd` is the
> Browse button → `getExistingDirectory` flow. Result: we never
> have to write a "is this string a valid path?" validator, nor
> handle a half-typed-mid-keystroke state, nor decide what to do
> with `~` or env-var paths. Constrain the input shape upstream
> and the downstream code carries less responsibility for free.

---

### 7.49 Two regressions from §7.48 — and what they teach

Right after §7.48 shipped, the user reported two bugs in the first
real-world Test Skill run from a non-launch cwd
(`C:\work\temp`):

1. *"Even Prefix Skill Name checkbox being checked, it didn't add
   `/<skill-name>` as prefix to the prompt box."*
2. *"ERROR: cannot access local variable 'run_cwd' where it is
   not associated a value."*

Both fixed in one iteration. Worth chronicling because they're
two distinct failure shapes from the same kind of code change.

**Bug 2: use-before-assignment in `_on_run`.** The §7.48 patch
referenced `run_cwd` in the diagnostic preface block but only
assigned it later (just before the worker-thread spawn). Standard
Python `UnboundLocalError`, surfaced via `_on_run`'s top-level
`except Exception` → `status_label.setText(f"ERROR: {e}")`. The
fix was a one-line move of the assignment upward, in front of the
first reference, with a comment noting the prior trap so the next
patch doesn't re-introduce the gap.

> Lesson: when adding a new local variable that needs to be
> visible across several blocks inside the same function,
> **assign it at the top of the block of work it belongs to** —
> not at the latest possible point. The "minimize variable
> scope" instinct from Java/C++ doesn't help in Python where
> there's no block scope; all you get from late assignment is
> a use-before-assignment crash if a later edit reorders things.

**Bug 1: checkbox state and buffer content diverged.** The
Prefix Skill Name feature was implemented as:

* Seed `/<skill> ` into the prompt buffer at construction time.
* React to checkbox `toggled` events by adding or removing the
  prefix.
* Nothing at Run time.

This works only as long as the user toggles the checkbox to
manage the prefix. The real-world flow — *select all, retype your
own prompt, click Run* — silently wipes the prefix while the
checkbox stays ticked. The checkbox's claim ("my prompt always
has the prefix") and the buffer's actual content diverge with no
warning. From the user's seat, the checkbox is "broken."

**Fix.** Move the source of truth from buffer state to checkbox
state, *evaluated at Run time*. In `_on_run`, after stripping the
prompt:

* If the checkbox is checked and the prompt doesn't already start
  with `/<skill>` (or `/<skill> `, or `/<skill>\n`), prepend
  `/<skill> ` to the prompt string sent to `claude`.
* Also rewrite the editor buffer to match (via the cursor API, not
  `setPlainText`, so undo history is preserved). The user sees the
  prompt that is actually sent — keeps the UI honest.
* Idempotent: a second pass over an already-prefixed buffer does
  nothing.

The construction-time seeding stays — it's a good first-paint cue
that the prefix is the default — but it's no longer load-bearing.
If the user wipes it, Run restores it.

> Lesson: when a checkbox / toggle controls a *property* of some
> data the user can also edit directly, that property must be
> **re-evaluated at the moment of use**, not just on toggle
> events. Construction-time seeding is fine as a hint; it can't
> substitute for an invariant check at the action boundary. The
> action boundary is where the contract becomes user-visible —
> any divergence before that point is recoverable; divergence
> at it is the bug.

**Bonus: the "empty log file" diagnostic question.** The user
also asked why the log file was empty. Triaged in conversation
rather than coded for, because the answer is operational, not
behavioral. Three real possibilities (left documented here so the
next reader of an "empty log file" report has a checklist):

1. **Truncation timing.** `configure_logging()` opens the file
   with `mode="w"` — every launch wipes the previous run's log.
   If the user launched, opened the log, then ran something, the
   reading happened before the writing.
2. **Wrong file.** `log_file_path()` resolves the directory
   containing the entry script via `sys.argv[0]`. If
   `sys.argv[0]` doesn't reflect the obvious `main.py` location
   (PyInstaller bundle, frozen exe, IDE launcher), the log writes
   land somewhere unexpected. **Help → Open log folder** is the
   authoritative way to find it — wired in `main_window.py` via
   `QDesktopServices.openUrl(log_file_path().parent)`.
3. **File open failed.** `logging_setup.py:79-86` catches `OSError`
   on the FileHandler constructor and falls back to
   `StreamHandler(sys.stderr)` — visible in the launching terminal
   but absent from any file. Read-only filesystem, lock contention,
   or a stale handle from a crashed prior instance can trigger
   this.

---

### 7.50 Skip permission prompts — letting Test Skill runs actually *do* things

**User observation that drove it.** After the §7.48 / §7.49 fixes,
the first real-world run with cwd set to `C:\work\temp` and prompt
*"create a simple Python script (just print 'Hello!')"* returned:

> The write was not approved. Let me know if you'd like me to
> retry, or to place the file at a different path.
>
> ★ Insight ─────────
> Claude Code asks before writing new files unless you've
> pre-approved the path in .claude/settings.json — the
> fewer-permission-prompts skill can automate that for paths you
> trust. …

Nothing in our app was wrong — `claude` faithfully reported that
`--print` mode (no human-in-the-loop) refuses tool calls without
explicit pre-approval. But the dialog's value proposition is
"test your skill"; a category of skill (scaffolders,
file-emitters, anything that *does* things) hits a permission
wall every time and looks broken.

**Decision: per-window opt-in checkbox.** Added
`Skip permission prompts` next to the existing Continue
conversation / Prefix Skill Name checkboxes. When ticked, the next
Run appends `--dangerously-skip-permissions` to argv;
`claude` auto-approves every tool call for that invocation.

**Why per-window, not a global setting.** Two distinct trade-offs:

* **Trust radius.** "I trust this prompt + this cwd combination"
  is a *contextual* assertion, not a *standing* one. The user
  might happily auto-approve in a `C:\work\temp` scratch directory
  but not when testing a Project skill rooted in a real repo.
  Per-window scope makes that contextual judgement explicit on
  every Run.
* **Forgetting cost.** A global "always skip permissions" toggle
  in Settings would silently apply to every window — including
  ones a user opens days later, against directories they
  haven't thought about, with prompts copied from somewhere
  untrusted. Per-window state resets to OFF when the dialog
  closes, so the *next* window opens safe by default. The cost
  of an extra click per session is small; the cost of a
  set-and-forget mistake is large.

**Why default OFF.** Mirrors `claude --print`'s own safe
default — denying tools without approval is the right starting
position for an unattended subprocess. A user who needs the flag
will check the box and immediately see the diagnostic preface
echo `--dangerously-skip-permissions  (auto-approving every tool
call this run)` in Raw Output, providing one last "are you sure?"
visual cue.

**Why echo the flag verbatim in the diagnostic preface.** The
flag name *is* the warning. Anthropic deliberately chose the
long `dangerously-` prefix so the string is hard to overlook in
a shell history or a log. Surfacing it character-for-character
in Raw Output preserves that signal; rephrasing as "skip safety
checks" or "auto-approve" would soften it and defeat the
upstream design choice.

**Wiring summary.**

* `skill_introspect.build_claude_command(..., skip_permissions=False)`
  — new kwarg, appends `--dangerously-skip-permissions` when True.
  Default preserves the pre-§7.50 invocation shape byte-for-byte.
* `TestSkillDialog.skip_perms_checkbox` — `QCheckBox` next to
  Continue and Prefix. Default unchecked. Per-dialog state, no
  persistence.
* Log line + diagnostic preface line both rendered when the flag
  is in effect; both omitted when it's not, so the no-feature
  case reads unchanged.

**Insights worth keeping.**

> Preserve the upstream's warning language verbatim. Anthropic
> chose `--dangerously-skip-permissions` instead of a shorter
> flag name as a UX device. Our checkbox label and our
> diagnostic line both echo it, instead of paraphrasing into
> something friendlier. Friendlier *is* the failure mode here —
> the long, uncomfortable wording is the safety mechanism.

> Per-window vs. global isn't just a code-architecture
> question. It's a *trust-radius* question. Defaults that
> survive across sessions need a higher bar than defaults
> scoped to one dialog instance, because the user's mental
> model at config time may not match their mental model at
> use time. For anything labeled "dangerously," scope to the
> narrowest unit that still serves the use case — which
> for a testing dialog is the dialog itself.

> The graceful-denial path from `claude --print` is a feature,
> not a workaround. It's tempting to read "tool was denied"
> as a bug to route around. The denial is `claude` honoring
> its own contract: no surprise side-effects in non-interactive
> mode. The right design layer to relax this is our UI (where
> the user can see and consent), not somewhere lower (an env
> var, a wrapper script). §7.50 stays inside the
> "expose the flag explicitly, default off" pattern; it never
> shells around `claude`'s default behavior.

---

### 7.51 Cancel-on-unsaved-changes leaves the wrong skill selected

**User observation that drove it.** *"Select a skill on the left
viewer, then click one .md file, say `a.md`, modify it (without
save). Now select another skill from the left panel. The
'Unsaved changes' message shows up, click 'Cancel'. The other
skill got selected but the Editor and Preview still display the
content of file `a.md`. Ideally, `a.md` should be selected (since
the user clicked Cancel on the Unsaved changes message)."*

The bug is the same shape as §7.25 (cancel-discard restoring the
file-tree row), but on a different panel. §7.25 fixed it for the
*middle* pane (`FileTreePanel`); the *left* pane (`SkillListPanel`)
was never wired for the same restore gesture, so cancelling a
cross-skill switch left the highlight on the row the user just
moved to even though no panel below it had updated. From the
user's seat: the selection lied.

**Asymmetry root cause.** `FileTreePanel` already exposed
`select_path()` for programmatic restoration without re-firing
`file_activated`. `SkillListPanel` had no equivalent — every
selection change was a user gesture, so there was no need to
suppress the `skill_selected` signal during a restore. The §7.25
fix only addressed half the surface.

**Fix.** Add `SkillListPanel.select_skill(skill)` mirroring
`FileTreePanel.select_path()`:

```python
def select_skill(self, skill: Skill) -> bool:
    self.tree.blockSignals(True)
    try:
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                stored = item.data(0, Qt.UserRole)
                if isinstance(stored, Skill) and stored.path == skill.path:
                    self.tree.setCurrentItem(item)
                    self.tree.scrollToItem(item)
                    return True
        return False
    finally:
        self.tree.blockSignals(False)
```

And teach `MainWindow.on_skill_selected` to call it when the
discard confirmation is cancelled:

```python
def on_skill_selected(self, skill: Skill) -> None:
    if not self.editor_panel.confirm_close():
        if self._current_skill is not None:
            if not self.skill_list.select_skill(self._current_skill):
                self.skill_list.clear_selection()
        else:
            self.skill_list.clear_selection()
        return
    ...
```

**Why `blockSignals(True)` is load-bearing.** Without it,
`setCurrentItem` re-fires `skill_selected`, which calls
`on_skill_selected` again, which sees a clean editor (because the
user just clicked Cancel and nothing dirty was actually saved or
discarded) and proceeds — silently swapping in the skill the user
just cancelled away from. The signal block is what makes the
restore *truly* invisible.

> Lesson: when a *fix* is implemented for one panel but the
> coordinated gesture spans two panels, the fix needs to be
> repeated symmetrically. §7.25 documented half the rule; the
> other half lay dormant until a user found the second
> failure path. Whenever a "restore on cancel" landing comes
> in, check every panel `MainWindow` orchestrates, not just
> the panel where the bug was reported.

---

### 7.52 Save As… for the Test Skill Response and Raw Output tabs

**User observation that drove it.** *"Add new feature: Response
and Raw Output on the Test Skill window — add Save As…
functionality to save content to a file. Response tab: save to a
`.md` file. Raw Output tab: save to a `.txt` file."*

Plain feature request, but with three sub-questions worth thinking
through before writing the button: *where* does it live in the
tab strip, *what* exactly gets saved (rendered vs. source), and
*what* filename does the dialog suggest by default?

**Where: `QTabWidget.setCornerWidget(widget, Qt.TopRightCorner)`.**
A separate button row above or below the tabs would either
duplicate the *Run / Cancel / Clear* row below or visually compete
with the tab strip itself. Qt's corner-widget slot is purpose-built
for context-sensitive tab actions — the button pins into the
otherwise-empty space at the right of the tab caps, makes it
visually clear that the action belongs to *the currently selected
tab*, and stays out of the way until the tab actually has content.

**What: snapshot the markdown source, not the rendered output.**
The Response tab uses `QTextBrowser.setMarkdown(text)`. The
straightforward implementation would round-trip via
`QTextBrowser.toMarkdown()` at save time — but Qt 6's
`toMarkdown` is lossy in subtle ways: it re-normalizes whitespace,
strips trailing newlines, occasionally rewrites bullets, and
strips the `> ★ Insight ──────` boxes that appear in many
responses. The output looks similar but isn't byte-for-byte the
markdown `claude` actually produced.

Fix: capture the markdown *before* it hits the view, store as
`self._last_response_markdown`, save that string on Save As. Same
end-state Anthropic's playground would produce if the user
right-clicked "Save response as markdown."

Raw Output is the easy half — the tab is a `QPlainTextEdit`, so
`toPlainText()` is already the truth.

**What filename:** timestamped, rooted at the dialog's per-window
cwd, derived from the skill name:

```python
def _default_save_filename(self, kind: str, ext: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in self._skill.name) or "skill"
    return str(self._cwd / f"{safe}-{kind}-{ts}.{ext}")
```

Result: `my-skill-response-20260512-141533.md`. Timestamp prevents
accidental overwrites when the user runs multiple tests in a row;
the safe-char filter strips any path-hostile characters from a
skill name (e.g. a plugin skill like `Foo/Bar v2`).

**Per-tab enable/disable.** The button is meaningless when its
target tab is empty. The slot `_update_save_btn_state` re-reads
the current tab's content on three events: `tabs.currentChanged`
(switched tabs), `_set_response_markdown` (new render arrived),
and `_clear_run_views` (clear was clicked). The button starts
disabled at construction and lights up the moment there's
something to save.

> Lesson: any "save the contents of this view" feature for a
> view that does its own markdown / HTML / rich-text rendering
> should save the **source**, not what the view shows. Round-
> tripping through the view's `toMarkdown` / `toHtml` / `toX`
> introduces silent reformatting that diverges from what the
> upstream tool produced. Snapshot at the call site that hands
> data *to* the view, not at the call site that asks the view
> *for* its current state.

---

### 7.53 JSON envelope readability in Raw Output (Continue mode)

**User observation that drove it.** *"The content of Raw Output
doesn't take `\n` and `\n\n` into action — it's displayed as one
line."* The user pasted a multi-line `claude --output-format json`
envelope that had been crushed into a wall of `…\\n…\\n…` because
the Raw Output tab is a `QPlainTextEdit` and renders the file
exactly as bytes: a JSON string literal's `\n` escape sequences
are *characters*, not real line breaks.

In §7.46 (Continue mode), Continue runs were switched from
`--output-format text` to `--output-format json` so the dialog
can parse the resume token. The side effect: the envelope is a
single physical line, with the *human-readable* content packed
into a `result:` field where every newline is escaped. Raw
Output dutifully showed exactly what claude emitted — which is
the right behavior for a "raw" tab, but punished readability.

**Fix.** When the run was a Continue (`_last_run_was_continue`),
parse the envelope, write *two* sections to Raw Output:

1. **Pretty-printed JSON** (`json.dumps(envelope, indent=2,
   ensure_ascii=False)`) — preserves the envelope structure so
   the user can audit session ids, costs, tool-call summaries.
2. **Decoded result section** — the `result` field rendered with
   real newlines, preceded by a divider:
   `\n---- decoded result (newlines rendered) ----\n`.

Non-Continue runs (plain `--print` text) fall through to the old
`_append_raw(stdout)` path unchanged — there's no JSON envelope
to unpack, so adding a divider would be confusing.

**Why two sections instead of replacing.** The pretty JSON is the
*ground truth* — what claude actually returned. The decoded
result is a *convenience view*. Replacing the envelope with just
the decoded content would erase the auditable structure that
makes Raw Output trustworthy (session id, cost, tool-call list).
Showing both pays one screenful of vertical space for full
information; the user can scroll past the JSON if they only want
the prose.

**Wiring detail.** The branch happens at the `_on_worker_result`
hand-off, gated on `self._last_run_was_continue`. The flag is
snapshotted at *click time* (in `_on_run`) so a mid-run toggle
of the Continue checkbox doesn't change how the in-flight result
is rendered.

> Lesson: when you switch a CLI from a human-friendly output
> format to a machine-friendly one (text → JSON) for a feature
> like §7.46, the human side of the pipe needs a complementary
> reverse transform somewhere. Otherwise you've broken
> readability to gain machine-readability — pure regression
> from the user's seat. Two views (raw envelope + decoded
> content) is usually the lowest-cost fix, because both
> audiences get what they need from the same pane.

---

### 7.54 Save As… visual polish — height match and the "white text" trap

**User observation that drove it.** Two issues, both about the
new corner-widget Save As… button:

1. *"The height of Save As… is different (short) from the tab
   control's title or caption."*
2. *"May change Save As… control color — now it looks 'white'
   text."*

Same control, two distinct visual bugs.

**Bug 1: height mismatch.** Initial padding was `4px 10px` (~22-24
px tall); the inner `QTabBar` rendered tabs with `padding: 6px
18px` (~27-28 px tall). The corner widget sat shorter than the tab
caps, looking detached.

Fix: bump padding to `6px 14px` *and* snap the button's height to
the tab bar's reported height once both tabs exist:

```python
bar_height = self._response_tabs.tabBar().sizeHint().height()
if bar_height > 0:
    self._save_as_btn.setFixedHeight(bar_height)
```

The `> 0` guard is for the case where the tab bar isn't laid out
yet (returns 0); the unconditional `setFixedHeight(0)` would
collapse the button. Critical timing detail: `sizeHint()` is only
accurate **after** the tabs have been added — querying it during
`_build_test_tab` before `addTab` calls returns 0.

**Bug 2: "white text" on light pane.** The initial stylesheet was
saturated blue text (`#2d6cdf`) on `background: transparent`. On
Windows ClearType, with a near-white pane background, the blue
glyphs' anti-aliased edges blend into the pane and the strokes
visually bleach toward "white" — *especially* the thinner glyphs
(`a`, `e`, the upper bowl of `s`). The button looked unreadable
even though the hex value was a real blue.

Fix: borrow the visual language of the existing Run / Cancel /
Clear row — filled `#f5f5f5` background, dark `#1a1a1a` text,
`#b0b0b0` border, blue *border* on hover (not blue text), and a
muted disabled state. Same shape pattern as `BUTTON_STYLE` in
`_styles.py`, just one notch quieter to mark "secondary action."

**Why filled instead of transparent.** Flat-transparent buttons
read clean on dark macOS toolbars but punish themselves on
light-pane Windows widgets: there's no fill to anchor the
ClearType subpixel rendering, so saturated foreground colors
bleach. A subtle fill gives the AA edges something to blend
against; the eye reads the glyph weight correctly.

**Smoke-tested measurements (headless `QT_QPA_PLATFORM=offscreen`):**
tab bar sizeHint height = 27 px, tab bar actual height = 27 px,
Save As… button height = 27 px. Heights match exactly.

> Lesson 1: when integrating a widget into a host strip (tab
> caps, header rows), the *correct* height isn't a hand-tuned
> padding value — it's whatever the neighbor's `sizeHint()` /
> `geometry()` says, queried **after** the neighbor's content
> is committed. Padding alone fights the host's intrinsic
> sizing; `setFixedHeight(neighbor.sizeHint().height())` makes
> the two snap together regardless of font / DPI / theme.

> Lesson 2: saturated foreground colors on transparent
> backgrounds are a ClearType anti-pattern. On Windows light
> themes, the AA edges of a saturated stroke bleach toward
> the background — making the glyph appear lighter than its
> hex value. Defaulting to dark text on a subtle fill is the
> shape Windows native controls use for the same reason.

---

### 7.55 Pre-trust the cwd in `~/.claude.json` — mirror Claude Desktop's "Trust this folder"

**User observation that drove it.** *"When I change the Working
Directory on Test Skill window, how can I tell Claude to have
Read permission and trust the selected location (like Claude
Desktop did)? So that I don't need to check 'Skip permission
prompts'."*

The `--dangerously-skip-permissions` toggle from §7.50 is a
sledgehammer: it bypasses *both* the trust-this-folder gate and
the per-tool-call permission prompts, and it does so for every
tool the run touches. Users who only want to grant the much
narrower "yes, you can read files in this directory" permission
have been forced to use the sledgehammer because there was no
narrower control.

**Why this gate is special.** When `claude -p` runs in a directory
the CLI hasn't seen before, the CLI opens an *interactive* prompt
asking the user to confirm trust. Our non-interactive `QProcess` /
`subprocess.Popen` invocation has no stdin to answer with — the
run hangs until the 3-minute timeout fires (§7.50 timeout
backstop). That's why §7.50's skip-permissions checkbox was the
only practical workaround before this section.

**Anatomy of the gate.** The CLI persists per-directory trust in
`~/.claude.json` under:

```json
"projects": {
  "C:/work/claude_demo/Claude_Skills_Manager_GUI": {
    "hasTrustDialogAccepted": true,
    ...
  }
}
```

Setting that one boolean is exactly what Claude Desktop's
*"Trust this folder?"* dialog writes. Once true, future CLI runs
in that directory skip the trust prompt — including ours.

**Key-shape gotcha (Windows-specific).** The path key uses
**forward slashes** even on Windows (`C:/work/...`, not
`C:\work\...`). Writing with backslashes creates a duplicate
entry that the CLI ignores, so the interactive prompt keeps
firing and the run keeps hanging. Verified empirically against
the existing trusted entries in `~/.claude.json` — every Windows
key in the wild uses forward slashes.
`claude_trust.normalize_trust_key()` centralizes the slash-flip.

**Two-gate distinction worth keeping clear.** Trust and per-tool
permissions are separate gates inside the CLI:

* **Trust** gates whether the directory is allowed *at all*
  (`projects.<key>.hasTrustDialogAccepted` in `~/.claude.json`).
* **Permissions** gate which operations are allowed *once
  trusted* (`.claude/settings.local.json` per-project, or the
  blanket `--dangerously-skip-permissions` flag).

A per-project `permissions.allow: ["Read(./**)"]` *does not*
bypass the trust dialog — it controls a different gate. §7.55
only touches trust; §7.50's permission flag is still the
correct tool for per-tool-call auto-approval.

**Architecture.** New Qt-free domain module
`claude_skills_manager/claude_trust.py`:

* `claude_config_path() -> Path` — resolves `~/.claude.json` via
  `Path.home()` (works on Windows / macOS / Linux without
  inspecting `$HOME` / `%USERPROFILE%` directly).
* `normalize_trust_key(path) -> str` — `str(path.resolve()).replace("\\", "/")`.
  The load-bearing forward-slash normalization.
* `is_path_trusted(path) -> Optional[bool]` — **tri-state**:
  `True` (explicitly trusted), `False` (entry missing or flag
  false), `None` (`~/.claude.json` doesn't exist at all). `None`
  is distinct from `False` because the caller's response differs:
  for `False` it should prompt; for `None` it should leave the
  file alone — writing a one-key stub would erase per-key state
  the CLI hasn't written yet but will need on first run.
* `mark_path_trusted(path) -> None` — read → mutate the single
  key → write to a sibling temp file → `os.replace`. Same-
  directory temp is intentional: `os.replace` is only
  guaranteed atomic when source and dest are on the same
  filesystem, and on Windows that means the same volume.
  Falls through to `FileNotFoundError` when the config is
  missing (caller decides — never auto-creates a stub).

The module lives in the Qt-free seam alongside `models.py` /
`scanner.py` / `skill_md.py`. UI code in `ui/test_dialog.py`
handles the user-facing prompt; the domain module just touches
the JSON file.

**UI wiring (two call sites).** Per the action-boundary rule from
§7.49, the *load-bearing* check goes at Run-click; the Browse
gesture gets a proactive hint:

```python
# _on_browse_cwd — hint after a new directory is picked
self._ensure_cwd_trusted(new_cwd, ask_user=True)

# _on_run — gate (skipped when skip_perms is on, since
# --dangerously-skip-permissions already bypasses the CLI's
# trust gate from inside)
if not skip_perms and not self._ensure_cwd_trusted(self._cwd, ask_user=True):
    self.status_label.setText("Trust required for this directory — Run cancelled.")
    return
```

`_ensure_cwd_trusted` shows a `QMessageBox.question` with the
target directory and a one-line explanation cross-referencing
Claude Desktop; on accept it calls `mark_path_trusted`; on
decline it returns False and the run is cancelled cleanly *before*
any UI side effects (status text, button states) so the user
isn't left looking at a half-started dialog.

**Why the `None` branch returns True (pass-through).** When
`~/.claude.json` doesn't exist at all, the user has never run
`claude` interactively. The right behavior is to *let the CLI
initialize itself* — including writing its own first-run trust
prompt — rather than us writing a stub file that omits every
other top-level key the CLI expects. The dialog launches `claude`,
which writes the file with proper structure, and the user can
re-Browse from that point on.

**Race window with the live CLI.** `~/.claude.json` is updated by
the live CLI itself (e.g. bumping `lastSessionId` after a run).
If the CLI writes between our read and our `os.replace`, we
overwrite that write. Same risk Claude Desktop runs with —
accepted by both tools as a low-frequency hazard. Mitigation:
the GUI never holds the file open between operations; the
read-mutate-replace window is microseconds.

> Lesson 1: when a tool stores configuration in a JSON file
> owned by another tool, **match the storage convention
> exactly**, including key-encoding subtleties (forward vs.
> backslashes, case folding, trailing slashes). A duplicate
> entry that *looks* right but is keyed slightly differently
> is silently ignored — the worst failure mode, because there's
> no error to catch and no log line to read.

> Lesson 2: the tri-state `Optional[bool]` return is exactly
> the right shape when the answer can be "explicitly yes,"
> "explicitly no," or "I can't determine." Collapsing it to
> `bool` forces a wrong choice at every call site: either you
> auto-trust on a missing config (creates a stub the CLI will
> trip over) or you nag-prompt the user every launch ("can't
> read config — trust anyway?"). Three states encode the
> domain; the caller branches on what it should do.

> Lesson 3: action-boundary enforcement applies to *external*
> side effects too. §7.49 framed it for in-dialog data; §7.55
> extends it across a process boundary. The same logic — the
> Browse-time prompt is a hint, the Run-click prompt is the
> contract — is what keeps the gesture reliable even if the
> user changes their mind between Browse and Run.

---

### 7.56 Prev/Next navigation in the image viewer

**User observation that drove it.** *"Image view window — to have
`<-` and `->` (or similar icons) to display the previous or next
image under the folder."*

The image dialog had a complete zoom/pan toolbar but no concept of
"the folder this image lives in." Every image was a one-shot
view; comparing two icons in the same folder meant closing the
dialog, double-clicking the next file, waiting for the dialog to
re-open, and trying to remember what the previous one looked like.

**Three sub-decisions worth keeping.**

**(1) Which extensions count as "image" for sibling discovery?**
The temptation is a hardcoded set — `{".png", ".jpg", ".gif",
".bmp", ".svg", ...}`. That's what `main_window.py` already had
for the initial-open decision. But: the top of `image_dialog.py`
imports QtSvg defensively (`try: from PySide6 import QtSvg`); on
a stripped PySide6 build that import fails silently and SVG
becomes un-loadable. A hardcoded set would let the user page to
a `.svg` that immediately fails the QPixmap load.

Fix: query `QImageReader.supportedImageFormats()` at runtime. The
answer reflects what Qt's currently-loaded plugins can actually
decode, including the SVG-or-not state. Computed at call time
(not module-import time) so the lazy SVG import has settled.

**(2) Why dialog-level `QShortcut` instead of `keyPressEvent`?**
The existing zoom shortcuts (`+`, `-`, `0`, `F`, `Esc`) live in
`keyPressEvent` and work fine. Left/Right arrows are different:
`QGraphicsView` inherits from `QAbstractScrollArea`, which
**consumes** plain arrow keys to scroll the viewport. Once the
user clicks or pans the image, focus moves into the view, and any
`keyPressEvent` override on the dialog never sees the arrow key.

`QShortcut(QKeySequence(Qt.Key_Left), self)` is bound at the
dialog level and bypasses focus-based key routing — fires
regardless of which child widget has focus. The zoom shortcuts
keep working from `keyPressEvent` because `+/-/0/F/Esc` aren't
consumed by `QGraphicsView`; nav is the case that needed the
escape hatch.

**(3) Should navigation failure close the dialog?** The original
`_load` rejected the dialog on null pixmap because the alternative
was a blank viewer — no fallback content. For *navigation*,
there's a perfectly good previous image still on screen. Failing
to load the *new* image shouldn't punish the user by closing
their window.

Fix: refactor `_load` to return `bool`. The initial-load path in
`__init__` defers a `reject()` on False (preserves the legacy
behavior); the navigation path just stays on the current image:

```python
def _navigate_to(self, new_index: int) -> None:
    if not (0 <= new_index < len(self._siblings)):
        return
    target = self._siblings[new_index]
    if not self._load(target):
        return  # _load already showed the warning
    self._index = new_index
    self.setWindowTitle(target.name)
    self._update_nav_state()
```

**Toolbar layout.** `◀  N / M  ▶` group on the left of the
toolbar, then `| Fit  100%  −  +  |  Zoom: 100%  Wpx×Hpx`. The
position label between the arrows (Apple Photos / Windows Photos
convention) gives the user constant feedback about where they are
in the run — *"image 3 of 17"* is much friendlier than a bare
disabled-button cue. Buttons are 36 px (narrower than the 48 px
zoom buttons) so the trio doesn't crowd the strip.

**Boundary behavior.** Prev disabled at index 0; Next disabled at
the last index; both disabled with `—` label when there's only one
image in the folder. Keyboard shortcuts share the same boundaries
via the same `_navigate_to` gate, so pressing `Left` at index 0
is a silent no-op (not an audible error chime, which Qt would
default to).

**Refit on every navigation.** `_load` re-arms `_fit_mode = True`
and schedules `fit_to_window` on the next event-loop tick.
Reasoning: a user who was zoomed in at 400% on one PNG has no
expectation that the same zoom should apply to a completely
different PNG (likely a different size). The natural mental model
is "show me this new image" from a fresh fit, same as opening it
directly.

**Sibling list is captured at `__init__`, not re-scanned on every
nav.** If the user adds or removes files in the folder mid-
session, they need to close + reopen to pick up the change.
Worth the simplicity: re-scanning on every nav would race with
user clicks (a slow network folder could stall the UI for
seconds), and the use case for "I added a file *while* paging"
is vanishingly thin.

**Smoke-tested (headless `QT_QPA_PLATFORM=offscreen`).** Six
cases: sibling-list filters out non-image files; alphabetical
sort; middle-image initial state has both buttons enabled;
forward-to-last disables Next; backward-to-first disables Prev;
out-of-bounds clicks are no-ops; single-image folder collapses
nav to `—` with both buttons disabled.

> Lesson 1: focus-based key routing is the silent failure mode
> of `keyPressEvent` overrides on dialogs that contain
> `QAbstractScrollArea` descendants. If the override "doesn't
> seem to fire," check whether a child widget is eating the
> key event before it bubbles up. `QShortcut` at the dialog
> level is the standard escape hatch — it's resolved against
> the dialog as a whole, not against the focus widget.

> Lesson 2: for "open one thing" → "navigate through similar
> things," derive the sibling list from the same predicate
> that decided what's openable in the first place. Hardcoding
> a separate filter drifts: extensions get added to one list
> and not the other, and "Next" surfaces files the original
> opener would have refused. `QImageReader.supportedImageFormats()`
> is the canonical predicate for image-openability; using it
> for both opener-filter and sibling-discovery keeps the two
> consistent by construction.

> Lesson 3: when refactoring a function to support a new call
> site with different error semantics (initial-load-must-close
> vs. navigation-must-stay-open), the right shape is *return
> a status, let callers decide policy* — not split the
> function into two near-duplicates. `_load(path) -> bool`
> serves both call sites; the policy difference (`reject()`
> vs. `return`) lives where it semantically belongs, one
> level up.

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
| `claude_skills_manager/skill_md.py` | 136 | YAML frontmatter + first-paragraph parser; chars-per-token estimator; **`strip_frontmatter`** with the §7.28 orphan-`---` defense — promoted from `editor_panel` so both the editor's Preview tab and the test dialog's Description tab share one implementation (§7.34) |
| `claude_skills_manager/skill_settings.py` | 134 | `skillOverrides` + `enabledPlugins` read/write; refuses to overwrite malformed JSON |
| `claude_skills_manager/claude_trust.py` | 137 | Qt-free seam for the §7.55 "Trust this folder" gate. `is_path_trusted(path)` returns tri-state `Optional[bool]` (True / False / None-for-missing-config), `mark_path_trusted(path)` writes `projects.<forward-slash-key>.hasTrustDialogAccepted=true` to `~/.claude.json` via atomic temp-file + `os.replace`. `normalize_trust_key` centralizes the load-bearing forward-slash conversion (Windows backslash keys are silently ignored by the CLI). Refuses to auto-create a stub config — falls through to `FileNotFoundError` so the CLI's own first-run initialization isn't pre-empted |
| `claude_skills_manager/scanner.py` | 321 | 3-source discovery (manifest-driven plugin discovery + legacy fallback), recursive project walk, dedup, state-population pass (per-skill scope) |
| `claude_skills_manager/skill_introspect.py` | 376 | Qt-free helpers for the test dialog (§7.34): example/summary extraction from a Skill's SKILL.md, `claude` CLI command construction, working-directory selection. Holds every decision a future runner needs (argv shape, cwd, example precedence) so the dialog can stay a presentation layer. Adds `find_claude_executable()` (§7.38, PATHEXT-aware via `shutil.which` + common-location fallback covering npm-global / pipx-local / installer paths) and `claude_path_diagnostic()` (renders a human-readable PATH report for the FailedToStart case) — used by both dialogs so QProcess always receives a fully-qualified path |
| `ui/main_window.py` | 883 | Toolbar (logo + Type + State filter groups, rich-text section labels, leading-magnifier search box per §7.23, refresh-icon button per §7.32, **Test Skill** button + Ctrl+T per §7.34, **Check Claude** button per §7.36), nested splitters, signal routing, `QSettings` persistence; validate-before-mutate on Choose-root; image-vs-text dispatch in `on_file_activated`; restores file-tree selection on cancelled-discard (§7.25); restores skill-list selection on cancelled cross-skill switch (§7.51 — calls `skill_list.select_skill(self._current_skill)`); search empty↔non-empty transition resets selection (§7.26 + §7.27); `_busy` context manager coordinates cursor + indeterminate progress bar + disabled-button busy state, initial scan deferred to first paint via `QTimer.singleShot` (§7.33); per-skill `_test_dialogs` registry + `open_test_dialog` raises-existing-or-creates (§7.34); single-instance `_check_claude_dialog` + `_on_check_claude_clicked` (§7.36); binds Windows taskbar icon via per-window IPropertyStore in `showEvent` (§7.22) |
| `ui/win32_taskbar.py` | 198 | Win32-only: per-window AppUserModelID + RelaunchIconResource binding via `SHGetPropertyStoreForWindow` and the `IPropertyStore` COM vtable. Pure ctypes (no comtypes / pywin32), silently no-ops on non-Windows or COM failure — see §7.22 |
| `ui/skill_list.py` | 662 | Grouped tree with type + state + per-type-context search filtering (§7.30), selection styling, per-type badge icons (full + faded variants — see §7.19/§7.20), right-click copy/open/Enable/Disable/**Test Skill…** menu; **always-on plugin / project context** as `name · context` (§7.29) plus on-collision name disambiguation scoped per group header (§7.18); tooltip enrichment for plugin-off, in-place `refresh_state`; correct paint-then-tag-DPR ordering (§7.23); programmatic `clear_selection()` for search-clear reset (§7.26); `test_skill_requested` signal emitted from the context menu so the right-click and the toolbar button converge on one `open_test_dialog` path (§7.34); programmatic `select_skill(skill)` (§7.51) mirrors `FileTreePanel.select_path` — blocks signals during `setCurrentItem` so a cancel-discard restore doesn't re-fire `skill_selected` and silently swap back to the rejected target |
| `ui/file_tree.py` | 79 | `QFileSystemModel`-backed tree, lazy attachment, `select_path()` for programmatic selection-restore on cancelled-discard (§7.25) |
| `ui/skill_info_panel.py` | 218 | SKILL.md size / mtime / line / char / token-estimate + Enable/Disable buttons in title row, word-wrapped form, plugin / non-binary read-only states |
| `ui/editor_panel.py` | 413 | Skill Description preview (+ description token estimate) + editor + conditional `.md` Preview tab (§7.24), content-based dirty, save/revert; `open_file` returns bool + `current_path()` accessor for cancel-discard selection-restore (§7.25); state-driven tab visibility via `_apply_tab_visibility` — Skill Description shown when a skill is selected, Editor added on file open, Preview added only for `.md` (§7.26); imports `strip_frontmatter` from `skill_md` (promoted in §7.34) for the doubled-terminator defense — single source of truth shared with the test dialog's Description tab |
| `ui/image_dialog.py` | 382 | Modal QGraphicsView image viewer — Ctrl+wheel zoom (cursor-anchored), drag-pan, Fit/100%/+/− toolbar, keyboard shortcuts. **Folder navigation** (§7.56): `◀ N / M ▶` group at the left of the toolbar pages through sibling images in the same folder. Siblings discovered via `QImageReader.supportedImageFormats()` (not a hardcoded extension set — keeps in lockstep with what `_load` will actually accept, including the lazy `QtSvg` import state), sorted by case-insensitive filename. Left / Right / PageUp / PageDown shortcuts bound via `QShortcut` at the dialog level so they survive focus moving into the view (which would otherwise consume plain arrow keys for viewport scrolling). `_load` returns `bool` so initial-load failure rejects the dialog (legacy behavior) while a navigation-load failure stays on the current image |
| `ui/test_dialog.py` | 1836 | Modeless `TestSkillDialog` (§7.34): header with skill metadata, four tabs (Description / Examples / Raw SKILL.md / Test). **Async runner — `subprocess.Popen` in a Python `threading.Thread` with Qt signal–marshalled result delivery** (§7.43). GUI stays responsive during the call: tick timer ticks 500ms-per elapsed-time / countdown, Cancel button kills the subprocess via shared lock, hard 3-minute timeout backstop. Playground-matching invocation shape (`["claude", "--print", prompt]`, no `stdin`/`cwd` overrides). **Test tab's bottom half is a nested QTabWidget** (§7.44): **Response** (QTextBrowser rendering claude's stdout as markdown — headers, lists, code fences, ★ Insight boxes all render cleanly) and **Raw Output** (QPlainTextEdit with the chronological diagnostic preface, raw stdout/stderr, and verdict markers). **Save As…** corner widget (§7.52) saves the currently-selected sub-tab to `.md` (Response, snapshotting the markdown *source* before Qt's lossy render round-trip) or `.txt` (Raw Output); default filename is `<skill>-<kind>-<YYYYMMDD-HHMMSS>` rooted at the per-window cwd; button enabled state tracks the current tab's content; height pinned to `tabBar().sizeHint().height()` so the corner button sits flush with the tab caps (§7.54). Per-verdict rendering: success path renders stdout to Response; failure/cancel/timeout paths render an italic notice pointing to Raw Output for details. **Continue-mode JSON envelope readability** (§7.53): when `_last_run_was_continue` is True, Raw Output gets two sections — pretty-printed envelope JSON (audit trail) followed by a `---- decoded result (newlines rendered) ----` divider and the result field with real newlines. Two worker → GUI signals (`_worker_result(exit_code, stdout, stderr)` and `_worker_failed(error_message)`) auto-route via `Qt::QueuedConnection`; `_worker_lock` guards the shared `_subproc` handle; `_is_running()` is the single source of truth for "is a run in progress." **`_append_raw` uses `QTextCursor.MoveOperation.End`** (the strict enum form required by PySide6 6.5+) — the legacy shorthand was the root cause of every prior "hang" (§7.42). Heavy logging via module-level `_log()` to `sys.stderr` with `[worker]`/`[gui]` thread-origin tags. FailedToStart-style errors dump the §7.38 PATH diagnostic into the Raw Output tab. Outer `try/except` around `_on_run` defends against PySide6 silently swallowing slot exceptions (§7.37). **Trust-this-folder gate** (§7.55): `_ensure_cwd_trusted(path, ask_user=True)` calls into the Qt-free `claude_trust` module to check/write `~/.claude.json`; fires both at `_on_browse_cwd` (proactive hint) and at `_on_run` (action-boundary enforcement, skipped when `skip_perms` is on since `--dangerously-skip-permissions` already bypasses the CLI trust gate); on decline, Run is cancelled before any UI side effects |
| `ui/check_claude_dialog.py` | 675 | Modeless `CheckClaudeDialog` (§7.36 + §7.37 + §7.38 + §7.39 + §7.42) — two-step baseline check. **Step 1** auto-runs `claude --version` on `showEvent` with 10s timeout (binary-only test, no auth/network). **Step 2** is `claude -p "Reply with OK"` (manual button, 90s timeout, full round-trip). Per-step color-coded status verdicts; shared output pane; Copy Command buttons for both invocations. Auto-run moved from `QTimer.singleShot(0, …)` to `showEvent` for deterministic firing; `try/except` wraps `_start_run` + I/O slots with `traceback.format_exc()` written into the pane on any failure — defense against PySide6 silently swallowing exceptions from queued slot calls (§7.37). Colored "Resolved `claude` at: …" banner at the top shows the located binary path; FailedToStart dumps the full `claude_path_diagnostic` into the pane (§7.38). `closeWriteChannel()` after `start()` to signal stdin EOF (§7.39). **`_append_output` uses `QTextCursor.MoveOperation.End`** (strict PySide6 6.5+ enum form); the previous `try/except: pass` wrapper around it was removed because it was hiding the AttributeError that caused six iterations of debugging misdirection (§7.42) |
| `ui/code_editor.py` | 98 | `QPlainTextEdit` + line numbers + current-line highlight |
| `ui/syntax.py` | 94 | Python / JSON / Markdown highlighters |
| `ui/_styles.py` | 38 | Shared QSS constants (currently `BUTTON_STYLE`); single source of truth for per-widget stylesheet snippets, per the §8.5 "stylesheets scoped per widget" convention |
| `ui/_icons.py` | 218 | Programmatic small UI icons — `search_icon()` (§7.23), `refresh_icon()` (§7.32, standard clockwise-arrow with filled arrowhead), and `test_icon()` (§7.34, stroked Erlenmeyer-flask silhouette for the Test Skill button). Separate from `app_icon.py` which is the brand logo. Same paint-then-tag-DPR pattern across all icons |
| `ui/app_icon.py` | 132 | Programmatic three-shapes composite logo (circle + square + diamond in the per-type palette); three layers over a shared physical-pixel painter — `app_icon()` for window-icon multi-size pack, `app_logo_pixmap(logical_size)` for in-window toolbar use with HiDPI DPR, `write_logo_ico(path)` for on-disk .ico used by the Windows taskbar binding — see §7.21, §7.22 |
