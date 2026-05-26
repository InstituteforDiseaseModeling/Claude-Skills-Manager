"""Modal dialog for creating a new Global or Project skill.

Three inputs (Type, Name, Description), live validation, a live path
preview, and a hard gate on Create until everything is valid. On
accept the dialog calls
:func:`~claude_skills_manager.skill_create.create_skill` and exits;
the caller (:class:`~claude_skills_manager.ui.main_window.MainWindow`)
handles the post-create refresh + select + open-in-editor flow.

**Per-type asymmetry.** Global is always available. Project is
always available too — the dialog hosts its own editable
"Project root" field (prefilled with the main window's current
root when one is set, otherwise empty), so the user can type or
browse to any folder without bouncing back to the main window.
The dialog's per-instance root does NOT propagate back to the
main window — matches the test_dialog's per-window Working
Directory idiom (§7.48). Plugin is permanently disabled with a
tooltip pointing at the upstream marketplace authoring path,
matching the existing per-type asymmetries (Enable/Disable
§7.14, Delete recent work).

**Default selection.** When a project root *is* set, the dialog
defaults to Project — the user has explicitly configured a
project context, so the contextual scope is the right starting
guess. Falling back to Global only when no project root exists.
This matches the memory rule "default toward the richer/stateful
path when the user has invested in setting up that context.\""""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
    QVBoxLayout, QWidget,
)

from ..models import SkillType
from ..skill_create import (
    create_skill, is_skill_name_taken, scope_dir_for, validate_description,
    validate_skill_name,
)
from ._styles import BUTTON_STYLE


# Stylesheet scoped to the dialog. Same convention as the rest of
# this codebase (per-widget objectName selectors, not app-global) —
# see CLAUDE.md's "stylesheets scoped per widget" rule.
_DIALOG_STYLE = """
QLabel#fieldLabel {
    color: #333333;
    font-weight: 600;
}
QLabel#hintLabel {
    color: #777777;
    font-size: 9pt;
}
QLabel#errorLabel {
    color: #b04040;
    font-size: 9pt;
}
QLabel#pathPreview {
    color: #555555;
    font-family: Consolas, "Courier New", monospace;
    background: #f4f6fa;
    border: 1px solid #d8dde6;
    border-radius: 3px;
    padding: 4px 8px;
}
QRadioButton:disabled {
    color: #999999;
}
"""


class NewSkillDialog(QDialog):
    """Modal "New Skill" creation form.

    Constructed with the parent window's currently-selected
    project root (may be ``None``). The constructor argument is
    used only to *prefill* the dialog's own editable Project root
    field — once the dialog is open, the user can type or browse
    to any folder. Validation fires on every keystroke; Create is
    enabled only when every field is valid. On accept, the
    created ``SKILL.md`` path is exposed via
    :attr:`created_skill_md_path` so the caller can locate the
    new skill in the post-refresh scan result."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        project_root: Path | None = None,
        initial_type: SkillType | None = None,
    ) -> None:
        super().__init__(parent)
        self._project_root = project_root
        # When provided, ``initial_type`` overrides the project-root-
        # based default in ``_set_default_type``. Used by the group-
        # header right-click flow so right-clicking "Global" opens
        # the dialog with Global preselected even if a project root
        # is set (which would otherwise default to Project).
        self._initial_type = initial_type
        self.created_skill_md_path: Path | None = None

        self.setWindowTitle("New Skill")
        self.setModal(True)
        self.setStyleSheet(_DIALOG_STYLE)
        # Fixed-ish width: wide enough for a typical path preview,
        # narrow enough that the dialog reads as a focused form
        # rather than an editor surface. Height grows with content.
        self.setMinimumWidth(520)

        self._build_ui()
        self._connect_signals()
        self._set_default_type()
        # Initial validation pass so the Create button reflects
        # the empty-name state from the first frame, instead of
        # being enabled-then-disabled on the first keystroke.
        self._revalidate()

    # ----------------------------------------------------------- UI assembly
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)

        layout.addWidget(self._build_type_row())
        layout.addWidget(self._build_project_root_row())
        layout.addWidget(self._build_name_row())
        layout.addWidget(self._build_description_row())
        layout.addWidget(self._build_path_preview())
        layout.addStretch(1)
        layout.addWidget(self._build_buttons())

    def _build_type_row(self) -> QWidget:
        """Type radio group. Plugin is permanently disabled (plugin
        skills are authored upstream; see module docstring). Global
        and Project are both always enabled — Project is no longer
        gated on MainWindow having a project root, because the
        editable Project root field below the radios lets the user
        pick one inline. Validation gates submission, not selection."""
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        label = QLabel("Type")
        label.setObjectName("fieldLabel")
        outer.addWidget(label)

        row = QHBoxLayout()
        row.setSpacing(18)

        self._type_group = QButtonGroup(self)

        self._rb_global = QRadioButton("Global")
        self._rb_global.setToolTip(
            "Create at ~/.claude/skills/<name>/. Available in every "
            "Claude Code session.")
        row.addWidget(self._rb_global)
        self._type_group.addButton(self._rb_global)

        self._rb_project = QRadioButton("Project")
        self._rb_project.setToolTip(
            "Create at <project root>/.claude/skills/<name>/. The "
            "project root is set per-dialog below; defaults to the "
            "main window's current project root.")
        row.addWidget(self._rb_project)
        self._type_group.addButton(self._rb_project)

        self._rb_plugin = QRadioButton("Plugin")
        self._rb_plugin.setEnabled(False)
        self._rb_plugin.setToolTip(
            "Plugin skills can't be created from this GUI. Plugins "
            "are authored upstream and distributed via marketplace "
            "manifests; use /plugin in Claude Code to install them.")
        row.addWidget(self._rb_plugin)
        self._type_group.addButton(self._rb_plugin)

        row.addStretch(1)
        outer.addLayout(row)
        return wrap

    def _build_project_root_row(self) -> QWidget:
        """Editable project root field + Browse button.

        Only relevant when the Project radio is selected — the
        whole row hides on Global. The field is editable (the
        user explicitly asked for "type and change the path"
        capability), prefilled with the MainWindow's current
        ``project_root`` when one is set so the common case
        ("create a skill in the project I'm already in") takes
        zero extra clicks. The Browse button opens a folder
        picker rooted at the current field value (or fallbacks)
        so successive browses behave naturally.

        Changes here do NOT propagate back to the MainWindow's
        project root — this dialog's path is per-instance,
        matching the test_dialog's per-window Working Directory
        idiom (§7.48). One-off Project skill creation in a
        different folder shouldn't fork the user's main browsing
        context."""
        self._project_root_row = QWidget()
        outer = QVBoxLayout(self._project_root_row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        label = QLabel("Project root")
        label.setObjectName("fieldLabel")
        outer.addWidget(label)

        row = QHBoxLayout()
        row.setSpacing(8)

        self._project_root_edit = QLineEdit()
        self._project_root_edit.setPlaceholderText(
            "Path to the project folder (e.g. C:\\projects\\my-app)")
        if self._project_root is not None:
            self._project_root_edit.setText(str(self._project_root))
        row.addWidget(self._project_root_edit, 1)

        self._project_root_browse = QPushButton("Browse…")
        # BUTTON_STYLE's ``min-height: 22px`` + ``padding: 4px 12px``
        # makes every button render ~30 px tall, while a default
        # QLineEdit on Windows lands around 22 px — visibly mismatched
        # next to the project-root field. Append a second
        # ``QPushButton`` block in the same stylesheet so QSS-cascade
        # rules (later rule wins for the same selector + same property)
        # override just the two inflating properties; background /
        # border / hover / pressed / disabled all stay inherited from
        # BUTTON_STYLE so the button still reads as clearly clickable.
        # No ``setFixedHeight`` companion — mixing widget-property
        # height with QSS-driven height is what made the first
        # iteration of this fix silently no-op (the style engine
        # repainted at the QSS-implied size regardless of the widget
        # extent). Trust the QSS to do all the sizing.
        self._project_root_browse.setStyleSheet(
            BUTTON_STYLE
            + "QPushButton { min-height: 0; padding: 2px 12px; }"
        )
        self._project_root_browse.clicked.connect(
            self._on_browse_project_root)
        row.addWidget(self._project_root_browse)

        outer.addLayout(row)

        hint = QLabel(
            "The skill will be created under "
            "<code>&lt;project root&gt;/.claude/skills/&lt;name&gt;/</code>. "
            "Discovered only when this folder is selected as the "
            "project root in the main window.")
        hint.setObjectName("hintLabel")
        hint.setTextFormat(Qt.RichText)
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self._project_root_error = QLabel()
        self._project_root_error.setObjectName("errorLabel")
        self._project_root_error.setWordWrap(True)
        self._project_root_error.hide()
        outer.addWidget(self._project_root_error)

        return self._project_root_row

    def _build_name_row(self) -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        label = QLabel("Name")
        label.setObjectName("fieldLabel")
        outer.addWidget(label)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. my-new-skill")
        # Conservative max length matches the validator's cap so
        # the user can't even type past the limit. Same number —
        # change one, change the other.
        self._name_edit.setMaxLength(64)
        outer.addWidget(self._name_edit)

        hint = QLabel(
            "Lowercase letters, digits, and hyphens. Becomes the "
            "folder name and the SKILL.md <code>name:</code> field.")
        hint.setObjectName("hintLabel")
        hint.setTextFormat(Qt.RichText)
        hint.setWordWrap(True)
        outer.addWidget(hint)

        # Inline error label — hidden by default; populated by the
        # live validator with the first failing reason. Word-wrap on
        # because validation messages can run a sentence long.
        self._name_error = QLabel()
        self._name_error.setObjectName("errorLabel")
        self._name_error.setWordWrap(True)
        self._name_error.hide()
        outer.addWidget(self._name_error)

        return wrap

    def _build_description_row(self) -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        label = QLabel("Description")
        label.setObjectName("fieldLabel")
        outer.addWidget(label)

        self._desc_edit = QPlainTextEdit()
        self._desc_edit.setPlaceholderText(
            "Brief sentence Claude reads to decide whether to invoke "
            "this skill.")
        # ~3 lines tall — fits a sentence or two without giving the
        # impression that the description is "where to write the
        # skill." The body of SKILL.md is for that, post-create.
        self._desc_edit.setFixedHeight(82)
        outer.addWidget(self._desc_edit)

        hint = QLabel(
            "Required. Claude uses this to decide whether to invoke "
            "the skill — empty descriptions make a skill effectively "
            "invisible to the model router.")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self._desc_error = QLabel()
        self._desc_error.setObjectName("errorLabel")
        self._desc_error.setWordWrap(True)
        self._desc_error.hide()
        outer.addWidget(self._desc_error)

        return wrap

    def _build_path_preview(self) -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        label = QLabel("Will be created at")
        label.setObjectName("fieldLabel")
        outer.addWidget(label)

        self._path_preview = QLabel("—")
        self._path_preview.setObjectName("pathPreview")
        self._path_preview.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        self._path_preview.setWordWrap(True)
        outer.addWidget(self._path_preview)

        return wrap

    def _build_buttons(self) -> QWidget:
        # Custom QDialogButtonBox so we can keep the button styling
        # consistent with the rest of the app's BUTTON_STYLE — the
        # default QDialogButtonBox styling is platform-dependent
        # and visually distinct from every other button in this
        # codebase, which uses BUTTON_STYLE everywhere.
        box = QDialogButtonBox()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setStyleSheet(BUTTON_STYLE)
        self._cancel_btn.clicked.connect(self.reject)
        box.addButton(self._cancel_btn, QDialogButtonBox.RejectRole)

        self._create_btn = QPushButton("Create")
        self._create_btn.setStyleSheet(BUTTON_STYLE)
        self._create_btn.setDefault(True)
        self._create_btn.clicked.connect(self._on_create_clicked)
        box.addButton(self._create_btn, QDialogButtonBox.AcceptRole)

        return box

    # ---------------------------------------------------------- wiring
    def _connect_signals(self) -> None:
        # Live revalidation on every change to any input. Cheap —
        # the validators are pure-Python regex + length checks, no
        # I/O. We re-evaluate everything (not just the changed
        # field) because validation has cross-field state:
        # is_skill_name_taken depends on the selected Type AND
        # project root, so a Type toggle has to re-run the name
        # validator too.
        self._name_edit.textChanged.connect(self._revalidate)
        self._desc_edit.textChanged.connect(self._revalidate)
        self._project_root_edit.textChanged.connect(self._revalidate)
        # Type toggle has TWO effects: (a) revalidate, (b) show /
        # hide the project-root row. Wire them as two slots on the
        # same signal rather than fusing them, so the per-effect
        # intent stays readable at the call site.
        for btn in (self._rb_global, self._rb_project):
            btn.toggled.connect(self._revalidate)
            btn.toggled.connect(self._apply_project_root_visibility)

        # Esc cancels — QDialog's default behavior wires this up
        # via the QDialogButtonBox reject role, but we also bind
        # a dialog-level QShortcut for symmetry with the rest of
        # the codebase's modeless-dialog convention.
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.reject)

    def _set_default_type(self) -> None:
        """Pick the initial Type.

        Precedence:

        1. Explicit ``initial_type`` constructor argument (used by
           the group-header right-click flow — the user clicked a
           specific section, so honor that even when the inferred
           default would have been different).
        2. Project when a project root is set in the main window —
           the user has explicitly invested in setting up that
           context, so the contextual scope is the natural guess.
        3. Global otherwise. Pre-selecting Project without a root
           would open the dialog with Create instantly disabled
           (empty Project root field fails validation), which
           looks broken at a glance.

        Plugin is never picked here — even if a future caller
        passes ``SkillType.PLUGIN``, the radio is permanently
        disabled, so ``setChecked(True)`` would silently no-op and
        leave the radio group with no selection. We fall through
        to the project-root-based default in that case."""
        if self._initial_type == SkillType.GLOBAL:
            self._rb_global.setChecked(True)
        elif self._initial_type == SkillType.PROJECT:
            self._rb_project.setChecked(True)
        elif self._project_root is not None:
            self._rb_project.setChecked(True)
        else:
            self._rb_global.setChecked(True)
        # Project-root row visibility must match the radio. Calling
        # this explicitly (rather than relying on the toggled signal
        # connection) handles the "checked-by-default radio doesn't
        # fire toggled" subtlety — Qt only emits toggled when the
        # state changes, and the initial setChecked may or may not
        # be a change depending on construction order.
        self._apply_project_root_visibility()

    # ---------------------------------------------------------- slots
    def _apply_project_root_visibility(self) -> None:
        """Show the project-root row iff Project is selected.

        Conditional-show (rather than always-visible + disabled)
        because a greyed-out "Project root" field above a Global
        selection is meaningless — Global skills have a fixed,
        non-configurable scope (``~/.claude/skills/``), so a
        visible-but-disabled control would invite the question
        "what is this for?" The ~40 px height jump on toggle is
        fine in a modal — nothing else is competing for the space."""
        is_project = self._rb_project.isChecked()
        self._project_root_row.setVisible(is_project)

    def _on_browse_project_root(self) -> None:
        """Open a folder picker for the Project root field.

        Start directory precedence: the current field value (if
        it resolves to an existing folder), then the MainWindow's
        prefilled root, then the user's home — most-specific first
        so successive browses behave naturally."""
        start: Path | None = None
        typed = self._project_root_edit.text().strip()
        if typed:
            try:
                candidate = Path(typed).expanduser().resolve()
            except OSError:
                candidate = None
            if candidate is not None and candidate.is_dir():
                start = candidate
        if start is None and self._project_root is not None:
            start = self._project_root
        if start is None:
            start = Path.home()

        chosen = QFileDialog.getExistingDirectory(
            self, "Choose project root", str(start))
        if not chosen:
            return
        # textChanged fires from setText → revalidate runs.
        self._project_root_edit.setText(chosen)

    def _effective_project_root(self) -> Path | None:
        """Return the Path object the dialog should currently treat
        as the Project root: the text in the field, expanded and
        resolved. Returns ``None`` if the field is empty or fails
        to resolve — the caller distinguishes "no path" from
        "invalid path" by combining this with
        :meth:`_validate_project_root_field`."""
        typed = self._project_root_edit.text().strip()
        if not typed:
            return None
        try:
            return Path(typed).expanduser().resolve()
        except OSError:
            return None

    def _validate_project_root_field(self) -> str | None:
        """Return ``None`` if the Project root field's current
        contents are an existing directory, otherwise a
        human-readable error message. Only consulted when the
        Project radio is selected — Global doesn't read this field."""
        typed = self._project_root_edit.text().strip()
        if not typed:
            return "Project root is required."
        path = self._effective_project_root()
        if path is None:
            return f"Cannot resolve path: {typed}"
        if not path.exists():
            return f"Folder does not exist: {path}"
        if not path.is_dir():
            return f"Not a folder: {path}"
        return None

    def _selected_type(self) -> SkillType | None:
        """Resolve the radio-group selection to a :class:`SkillType`.
        Returns ``None`` if nothing is selected (only happens
        transiently during construction; defensive guard)."""
        if self._rb_global.isChecked():
            return SkillType.GLOBAL
        if self._rb_project.isChecked():
            return SkillType.PROJECT
        return None

    def _revalidate(self) -> None:
        """Run every validator + the taken-name check, update the
        path preview, and enable Create iff everything passes.

        Field-level errors render inline (hidden when clean). The
        Create button's disabled state is the final gate, so a
        user can't accidentally submit through an Enter key while
        a field shows a red error message."""
        name = self._name_edit.text()
        description = self._desc_edit.toPlainText()
        skill_type = self._selected_type()

        name_err = validate_skill_name(name) if name else None
        desc_err = validate_description(description) if description else None

        # Project root validation runs only when Project is the
        # selected type — Global ignores the field entirely.
        root_err: str | None = None
        if skill_type == SkillType.PROJECT:
            root_err = self._validate_project_root_field()

        # Taken-ness check requires (a) a name that already passed
        # the pure-pattern validation, (b) a known skill type, and
        # (c) a resolvable scope directory — which for Project
        # means the root field also has to validate. Skip if any
        # of those prerequisites are missing; the other errors
        # will speak first.
        taken_err: str | None = None
        if (name and not name_err
                and skill_type is not None
                and not root_err):
            scope = self._resolve_scope_dir(skill_type)
            if scope is not None and is_skill_name_taken(name, scope):
                taken_err = (
                    f"A skill called '{name}' already exists "
                    f"in {scope}.")

        self._render_inline_error(
            self._name_error,
            taken_err or name_err if name else None)
        self._render_inline_error(
            self._desc_error, desc_err if description else None)
        # The project-root error renders only when Project is
        # selected — Global mode hides the whole row so the error
        # label inside it is invisible anyway, but clearing the
        # text avoids a stale message flashing if the user toggles
        # back to Project later.
        if skill_type == SkillType.PROJECT:
            self._render_inline_error(
                self._project_root_error,
                root_err if self._project_root_edit.text().strip()
                else None)
        else:
            self._render_inline_error(self._project_root_error, None)

        self._update_path_preview(name, skill_type)

        all_clean = (
            name
            and description
            and not name_err
            and not desc_err
            and not taken_err
            and not root_err
            and skill_type is not None)
        self._create_btn.setEnabled(bool(all_clean))

    def _resolve_scope_dir(
        self, skill_type: SkillType,
    ) -> Path | None:
        """Compute the scope directory (``.../.claude/skills/``)
        for the chosen type using the dialog's own state (its
        editable project root field for Project). Returns
        ``None`` on any failure — callers chain this with
        validation, so a None here just means "can't speak yet."
        """
        try:
            if skill_type == SkillType.PROJECT:
                root = self._effective_project_root()
                if root is None:
                    return None
                return scope_dir_for(skill_type, root)
            return scope_dir_for(skill_type, None)
        except ValueError:
            return None

    @staticmethod
    def _render_inline_error(label: QLabel, message: str | None) -> None:
        """Show/hide an inline error label. Centralized so the
        show + setText + ensure-not-stale sequence is consistent."""
        if message is None:
            label.hide()
            label.setText("")
            return
        label.setText(message)
        label.show()

    def _update_path_preview(
        self, name: str, skill_type: SkillType | None,
    ) -> None:
        """Compose a live "Will be created at" preview path.

        Renders as a placeholder ``<name>`` when the name field is
        empty so the user can still see the surrounding scope path
        and know which type they've picked. Falls back to ``"—"``
        if the type or scope can't be resolved (no radio selected,
        empty or invalid project root, etc.)."""
        if skill_type is None:
            self._path_preview.setText("—")
            return
        scope = self._resolve_scope_dir(skill_type)
        if scope is None:
            self._path_preview.setText("—")
            return
        leaf = name.strip() or "<name>"
        full = scope / leaf / "SKILL.md"
        # html.escape because the path is rendered as plain text
        # in a QLabel; on a path containing ``&`` Qt would parse
        # it as a mnemonic accelerator and silently drop it.
        self._path_preview.setText(html.escape(str(full)))

    def _on_create_clicked(self) -> None:
        """Submit handler. Re-runs validation through the domain
        layer (defense in depth — the live validators already
        gated the button, but a future code path could enable the
        button without going through ``_revalidate``), calls
        :func:`create_skill`, and on success stores the path +
        accepts. Failures render in the inline error labels rather
        than popping a QMessageBox — keeps the user in one
        editing context."""
        name = self._name_edit.text()
        description = self._desc_edit.toPlainText()
        skill_type = self._selected_type()
        if skill_type is None:
            # Should be unreachable — Create is disabled when no
            # type is selected — but bail rather than crash.
            return
        # The project_root passed to create_skill comes from the
        # dialog's field, not the MainWindow's value — this is the
        # whole point of the editable row. For Global it's
        # irrelevant (create_skill ignores it).
        effective_root = (
            self._effective_project_root()
            if skill_type == SkillType.PROJECT
            else None)
        try:
            path = create_skill(
                name=name,
                description=description,
                skill_type=skill_type,
                project_root=effective_root,
            )
        except FileExistsError as exc:
            # Race: name was free during live validation but a
            # competing process created the folder between then
            # and now. Surface inline.
            self._render_inline_error(self._name_error, str(exc))
            return
        except ValueError as exc:
            # Validator caught something live-validation missed
            # (shouldn't happen, but stay honest).
            self._render_inline_error(self._name_error, str(exc))
            return
        except OSError as exc:
            # Filesystem error — permission, disk full, etc.
            # Render against the path preview so the user
            # immediately sees which target was being written.
            self._render_inline_error(
                self._desc_error,
                f"Couldn't create skill: {exc}")
            return
        self.created_skill_md_path = path
        self.accept()
