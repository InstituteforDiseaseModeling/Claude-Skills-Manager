"""Right panel — Skill Description, Editor and (conditional) Preview tabs.

Owns dirty-state tracking, save/revert actions and binary-file detection.

Tab visibility is **state-driven** — a single ``_apply_tab_visibility``
method computes which tabs should appear given the current
``(_current_skill, _current_path)`` pair, instead of every entry point
flipping tabs ad-hoc. See §7.26 for the rationale (previous design left
Editor permanently visible, which surfaced as "tabs leak through the
no-skill state" on app start / Refresh / search-clear).

Tab visibility rules:

| State                                     | Visible tabs                            |
|-------------------------------------------|-----------------------------------------|
| No skill selected                         | *(tab bar hidden entirely)*             |
| Skill selected, no file open              | Skill Description                       |
| Skill selected, non-``.md`` file open     | Skill Description + Editor              |
| Skill selected, ``.md`` file open         | Skill Description + Editor + Preview    |

Per-tab semantics:

* **Skill Description** — renders the *selected skill's* ``SKILL.md``
  (frontmatter stripped) with a synthesized header: name + description +
  token count.
* **Editor** — raw text editor for the file the user clicked in the
  middle file tree.
* **Preview** — renders the *currently-open file* (live buffer,
  frontmatter stripped) as markdown. Distinct from Skill Description:
  this tracks the file under edit, not the skill."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..models import Skill
from ..skill_md import estimated_token_count, parse_skill_md_text
from ._styles import BUTTON_STYLE
from .code_editor import CodeEditor
from .syntax import highlighter_for_extension


class EditorPanel(QWidget):
    file_saved = Signal(Path)

    # Stable tab indices, used both for setCurrentIndex() calls and the
    # show/hide gating on the conditional Preview tab. Defining them as
    # class constants documents the layout in one place and prevents
    # off-by-one drift if a future tab is inserted.
    _DESCRIPTION_TAB_INDEX = 0
    _EDITOR_TAB_INDEX = 1
    _PREVIEW_TAB_INDEX = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._current_path: Path | None = None
        self._current_skill: Skill | None = None
        self._highlighter = None
        self._dirty: bool = False
        # Snapshot of the file's on-disk content. Dirty state is computed by
        # comparing the live buffer to this — typing the file back to its
        # original contents correctly clears the dirty flag.
        self._pristine_text: str = ""

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(_TAB_STYLE)
        self._build_description_tab()
        self._build_editor_tab()
        self._build_preview_tab()
        # Re-render the description from the live editor buffer when the
        # user switches BACK to that tab — picks up unsaved edits to SKILL.md.
        self.tabs.currentChanged.connect(self._on_tab_changed)
        # Initial paint: no skill, no file → tab bar entirely hidden.
        # Every subsequent state transition routes through _apply_tab_visibility.
        self._apply_tab_visibility()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

    # ------------------------------------------------------------- UI build
    def _build_description_tab(self) -> None:
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        self.tabs.addTab(self.preview, "Skill Description")

    def _build_preview_tab(self) -> None:
        # Renders the *currently-open file* as markdown — distinct from the
        # Skill Description tab, which always renders the *selected skill's*
        # SKILL.md. This tab only appears when a .md file is open.
        self.md_preview = QTextBrowser()
        self.md_preview.setOpenExternalLinks(True)
        self.tabs.addTab(self.md_preview, "Preview")

    def _build_editor_tab(self) -> None:
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        bar = QHBoxLayout()
        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet("color:#666;")
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setStyleSheet(BUTTON_STYLE)
        self.revert_btn.setEnabled(False)
        self.revert_btn.clicked.connect(self.revert_current)
        self.save_btn = QPushButton("Save")
        self.save_btn.setStyleSheet(BUTTON_STYLE)
        self.save_btn.setShortcut("Ctrl+S")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save_current)
        bar.addWidget(self.file_label, 1)
        bar.addWidget(self.revert_btn)
        bar.addWidget(self.save_btn)

        self.editor = CodeEditor()
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(10)
        self.editor.setFont(font)
        # Content-based dirty: dirty iff buffer != pristine snapshot. Immune
        # to QSyntaxHighlighter rehighlights (formats change, plain text
        # doesn't) and immune to "edit then type back" — both cases the
        # equality check returns the right answer.
        self.editor.textChanged.connect(self._reconcile_dirty)

        v.addLayout(bar)
        v.addWidget(self.editor)
        self.tabs.addTab(container, "Editor")

    # -------------------------------------------------------------- public API
    def show_skill(self, skill: Skill) -> None:
        """Switch the panel to ``skill``. Drops any previously-open file: the
        old file isn't in the new skill's tree, so leaving the Editor tab
        showing it would be confusing. Only the Skill Description tab is
        visible after this call — Editor reappears when the user clicks a
        file in the middle pane."""
        self._reset_file_state()
        self._current_skill = skill
        self._render_description()
        self._apply_tab_visibility()
        self.tabs.setCurrentIndex(self._DESCRIPTION_TAB_INDEX)

    def clear(self) -> None:
        """Reset to the empty 'no skill selected' state — tab bar hidden,
        all editors blank. Called at app start, on Refresh, and (via
        ``MainWindow``) when the user clears the search box."""
        self._reset_file_state()
        self._current_skill = None
        self.preview.setMarkdown("")
        self._apply_tab_visibility()
        self.tabs.setCurrentIndex(self._DESCRIPTION_TAB_INDEX)

    def open_file(self, path: Path) -> bool:
        """Open ``path`` in the editor. Returns ``True`` if the file is now
        showing in the editor, ``False`` if the request was rejected (user
        cancelled the unsaved-changes prompt, path isn't a regular file, or
        the read failed). ``MainWindow`` consumes the bool to restore the
        file-tree selection back to whatever the editor is *actually* showing
        — without this signal-back-up, the tree highlight ends up on the
        file the user clicked, but the editor still shows the previous file."""
        if self._dirty and not self._confirm_discard():
            return False
        if not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            QMessageBox.warning(self, "Open failed", f"Could not open {path}:\n{e}")
            return False

        self._current_path = path
        self._pristine_text = text
        # Detach previous highlighter cleanly before swapping document content
        if self._highlighter is not None:
            self._highlighter.setDocument(None)
            self._highlighter = None

        self.editor.setPlainText(text)
        self._highlighter = highlighter_for_extension(path.suffix, self.editor.document())
        # setPlainText fired textChanged -> _reconcile_dirty already, but the
        # explicit call here covers the no-change-from-empty edge case.
        self._reconcile_dirty()
        # _apply_tab_visibility decides whether Preview is visible based on
        # the new _current_path's suffix — see the tab visibility rules in
        # the module docstring.
        self._apply_tab_visibility()
        self.tabs.setCurrentIndex(self._EDITOR_TAB_INDEX)
        return True

    def current_path(self) -> Path | None:
        """The file the editor is currently displaying, or ``None`` if no file
        is open. Used by ``MainWindow`` to restore the file-tree selection
        when ``open_file`` is rejected — see that method's docstring."""
        return self._current_path

    def save_current(self) -> None:
        if self._current_path is None or not self._dirty:
            return
        buffer_text = self.editor.toPlainText()
        try:
            self._current_path.write_text(buffer_text, encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Save failed",
                                f"Could not save {self._current_path}:\n{e}")
            return
        # The buffer IS now the on-disk content — adopt it as the new pristine.
        self._pristine_text = buffer_text
        self._reconcile_dirty()
        self.file_saved.emit(self._current_path)

    def revert_current(self) -> None:
        if self._current_path is None:
            return
        try:
            text = self._current_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self._pristine_text = text
        self.editor.setPlainText(text)
        self._reconcile_dirty()

    def has_unsaved(self) -> bool:
        return self._dirty

    def confirm_close(self) -> bool:
        """Return True if the caller may proceed.

        If there are unsaved changes, prompt the user. On Discard we actually
        apply the discard — reverting the buffer to disk — so the dirty flag
        clears and subsequent switches don't prompt again.
        """
        if not self._dirty:
            return True
        if self._confirm_discard():
            self.revert_current()
            return True
        return False

    # --------------------------------------------------------------- internals
    def _apply_tab_visibility(self) -> None:
        """Single source of truth for which tabs the user can see, computed
        from ``(self._current_skill, self._current_path)``.

        Every state-changing entry point (``show_skill``, ``clear``,
        ``open_file``) calls this at the end so the tab bar reflects the
        new state without each call having to remember the full set of
        ``setTabVisible`` flips. Bar visibility itself is also toggled —
        hiding the whole bar when no skill is selected is the only way to
        avoid a vestigial empty strip above the panel."""
        has_skill = self._current_skill is not None
        has_file = self._current_path is not None
        is_md = has_file and self._current_path.suffix.lower() == ".md"

        bar = self.tabs.tabBar()
        bar.setTabVisible(self._DESCRIPTION_TAB_INDEX, has_skill)
        bar.setTabVisible(self._EDITOR_TAB_INDEX, has_skill and has_file)
        bar.setTabVisible(self._PREVIEW_TAB_INDEX, has_skill and has_file and is_md)
        bar.setVisible(has_skill)

    def _reset_file_state(self) -> None:
        """Clear file-specific state (open path, editor buffer, highlighter,
        dirty flag) while leaving ``_current_skill`` intact.

        Shared between ``clear()`` (full reset to no-skill) and
        ``show_skill()`` (skill switch — the old file isn't in the new
        skill's tree, so it has to go). Pulling this into one helper keeps
        the two call sites from drifting apart."""
        if self._highlighter is not None:
            self._highlighter.setDocument(None)
            self._highlighter = None
        self._current_path = None
        self._pristine_text = ""
        self.editor.clear()
        self.md_preview.setMarkdown("")
        self._set_dirty(False)

    def _on_tab_changed(self, index: int) -> None:
        # Refresh the markdown-rendering tabs lazily as the user lands on
        # them so unsaved edits in the editor buffer are reflected without
        # paying the parse cost on every keystroke.
        if index == self._DESCRIPTION_TAB_INDEX and self._current_skill is not None:
            self._render_description()
        elif index == self._PREVIEW_TAB_INDEX and self._current_path is not None:
            self._render_md_preview()

    def _render_md_preview(self) -> None:
        """Render the live editor buffer as markdown into the Preview tab.
        Strips a leading YAML frontmatter block (same as Description) so the
        rendered prose body is what actually shows up."""
        text = self.editor.toPlainText()
        self.md_preview.setMarkdown(_strip_frontmatter(text).strip())

    def _render_description(self) -> None:
        skill = self._current_skill
        if skill is None:
            self.preview.setMarkdown("")
            return

        raw = self._read_skill_md_for_preview(skill)
        # Re-parse name/description from whatever source we picked, so frontmatter
        # edits in the editor are reflected in the header without saving.
        if raw:
            metadata, fresh_desc = parse_skill_md_text(raw)
            name_field = metadata.get("name")
            name = name_field if isinstance(name_field, str) and name_field else skill.name
            description = fresh_desc or skill.description
        else:
            name, description = skill.name, skill.description

        body = _strip_frontmatter(raw).strip() if raw else ""
        parts: list[str] = [f"# {name}"]
        if description:
            parts.append(f"> {description}")
            # Token count for the description text itself (NOT the whole
            # SKILL.md — that's in the info panel). The description sits
            # in Claude's context unconditionally so its size matters
            # independently of the body.
            parts.append(
                f"*Description: ≈{estimated_token_count(description):,} tokens*")
        if body:
            parts.append("---")
            parts.append(body)
        self.preview.setMarkdown("\n\n".join(parts))

    def _read_skill_md_for_preview(self, skill: Skill) -> str:
        """If the editor is currently holding this skill's SKILL.md, return
        the in-memory buffer; otherwise read from disk."""
        if (skill.skill_md_path is not None
                and self._current_path is not None
                and self._current_path == skill.skill_md_path):
            return self.editor.toPlainText()
        if skill.skill_md_path and skill.skill_md_path.is_file():
            try:
                return skill.skill_md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        return ""

    def _reconcile_dirty(self) -> None:
        """Recompute dirty by comparing the live buffer against the on-disk
        snapshot. Cheap (microseconds for typical SKILL.md sizes) and
        immune to highlighter rehighlights — formats change but text doesn't."""
        if self._current_path is None:
            if self._dirty:
                self._set_dirty(False)
            return
        is_dirty = self.editor.toPlainText() != self._pristine_text
        if is_dirty != self._dirty:
            self._set_dirty(is_dirty)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        enabled = dirty and self._current_path is not None
        self.save_btn.setEnabled(enabled)
        self.revert_btn.setEnabled(enabled)
        if self._current_path is not None:
            mark = "● " if dirty else ""
            self.file_label.setText(f"{mark}{self._current_path}")
        else:
            self.file_label.setText("No file selected")

    def _confirm_discard(self) -> bool:
        ans = QMessageBox.question(
            self, "Unsaved changes",
            "Discard unsaved changes to the current file?",
            QMessageBox.Discard | QMessageBox.Cancel,
        )
        return ans == QMessageBox.Discard


# Strong selected-state contrast: bold + colored underline + lighter
# inactive tabs so the active tab is unambiguous on Windows.
_TAB_STYLE = """
QTabWidget::pane {
    border: 1px solid #c8c8c8;
    background: #ffffff;
    top: -1px;
}
QTabBar::tab {
    background: #ececec;
    color: #555555;
    padding: 6px 18px;
    border: 1px solid #c8c8c8;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
    min-width: 90px;
}
QTabBar::tab:hover:!selected {
    background: #f5f5f5;
    color: #1a1a1a;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1a1a1a;
    font-weight: bold;
    border-bottom: 3px solid #2d6cdf;
    margin-bottom: -1px;
}
"""


def _strip_frontmatter(md: str) -> str:
    """Drop a leading YAML frontmatter block — and any orphan bare ``---``
    lines that immediately follow it — so the rendered preview shows only
    the prose body.

    The "and orphan ``---`` lines" tail is load-bearing. Some SKILL.md
    files in the wild ship with a *doubled* terminator (``---\\nyaml\\n
    ---\\n---``) — an authoring slip where the writer typed three dashes
    twice at the end of the frontmatter. The code-review skill in the
    ``idm-agent-skills`` marketplace is one such file. Without eating the
    orphan, Qt's CommonMark parser reads the leftover ``---`` as the
    *opener* of a new YAML block and scans ahead for a closer. It finds
    one inside a ``` ```markdown ``` example block further down the file
    and hides every line in between — most of the document.

    See §7.28 for the parser-composition failure mode this guards
    against. The loop also gracefully handles a third or fourth
    terminator if anyone manages to triple-tap; bare ``---`` lines are
    horizontal rules either way, but stripping them at the head keeps
    the rendered output starting with a real heading."""
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
