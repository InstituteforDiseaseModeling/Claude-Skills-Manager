"""CRUD editor for the packaged AI tools table.

Opened from Resource → Edit Resource… in the main window. Lets the
user add / delete / modify rows of ``ai_tools.md`` and persist the
result. The dialog is modal; on accept, the main window re-runs
``_populate_resource_menu`` to reflect the edits.

Edits live in an in-memory ``list[AITool]`` snapshot copied at open
time. Form fields are captured into the current row on selection
change and on Save — but NOT on every keystroke; the dirty check
captures-then-compares at the close boundary so a half-typed change
is still detected. Same content-based-dirty discipline as
``EditorPanel`` (see CLAUDE.md), applied to a structured record
instead of a text buffer.

Close→Discard semantics are deliberately "revert-and-stay" rather
than "revert-and-close". A user who clicked "+ Add", filled nothing
in, and hit Close should not have to re-open the dialog from the
Resource menu to keep editing — the snapshot revert wipes the
unsaved row in place and the editor stays open at the last-saved
state. The only path that actually closes a dirty dialog is
"Discard (now clean) → Close again". Wording in ``_confirm_discard``
calls this out so the button isn't surprising.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QVBoxLayout, QWidget,
)

from ..ai_tools import AITool, load_ai_tools, save_ai_tools
from ._styles import BUTTON_STYLE

_logger = logging.getLogger("edit_resource_dialog")


class EditResourceDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Resource")
        self.setModal(True)
        self.resize(820, 480)

        loaded = load_ai_tools()
        self._tools: list[AITool] = list(loaded)
        # Snapshot is the comparison baseline for the dirty check.
        # Frozen dataclasses + list equality handles structural compare
        # without any per-field walking.
        self._snapshot: list[AITool] = list(loaded)
        self._current_idx: int | None = None

        self._build_ui()
        self._refresh_list()
        if self._tools:
            self.list_widget.setCurrentRow(0)
        else:
            self._set_form_enabled(False)
        # Final tick of button state after the construction cascade
        # settles. The auto-select above triggered _on_row_changed
        # which already called this, but a redundant call costs
        # microseconds and is resilient against future construction
        # paths that don't hit the row-changed code path.
        self._update_button_states()

    # ----------------------------------------------------------- layout
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_pane())
        splitter.addWidget(self._build_right_pane())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([260, 540])
        layout.addWidget(splitter, 1)

        # Save no longer closes the dialog (per user spec) — it
        # persists to disk, shows a confirmation, and leaves the
        # editor open. Close is the only exit.
        # ``QDialogButtonBox.Close`` carries RejectRole, so the
        # existing ``rejected`` signal wiring still routes through
        # the dirty-prompt guard in ``_handle_cancel``.
        # ``accepted`` fires when Save is clicked, but our handler
        # deliberately does NOT call ``self.accept()`` — that's what
        # keeps the dialog open across saves.
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Close)
        for btn in buttons.buttons():
            btn.setStyleSheet(BUTTON_STYLE)
        buttons.accepted.connect(self._on_save_clicked)
        buttons.rejected.connect(self._handle_cancel)
        # Save button starts disabled — enabled only when the model
        # diverges from the snapshot. Reference held on self so
        # _update_button_states can toggle it.
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setEnabled(False)
        layout.addWidget(buttons)

    def _build_left_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        self.add_button = QPushButton("+ Add")
        self.add_button.setStyleSheet(BUTTON_STYLE)
        self.add_button.clicked.connect(self._on_add_clicked)
        self.delete_button = QPushButton("− Delete")
        self.delete_button.setStyleSheet(BUTTON_STYLE)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.delete_button)
        button_row.addStretch(1)
        v.addLayout(button_row)

        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        v.addWidget(self.list_widget, 1)
        return pane

    def _build_right_pane(self) -> QWidget:
        pane = QWidget()
        form = QFormLayout(pane)
        form.setContentsMargins(8, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)
        form.setSpacing(8)

        self.name_edit = QLineEdit()
        # Live-update the list item label so the user can see the
        # name they're typing reflected in the left pane immediately.
        # Per-field textChanged also drives _update_button_states so
        # the Save button enables/disables in real time as content
        # diverges from / re-converges to the snapshot.
        self.name_edit.textChanged.connect(self._on_name_typed)
        self.name_edit.textChanged.connect(self._update_button_states)
        form.addRow("Name:", self.name_edit)

        self.main_edit = QLineEdit()
        self.main_edit.setPlaceholderText("https://…")
        self.main_edit.textChanged.connect(self._update_button_states)
        form.addRow("Main Website:", self.main_edit)

        self.docs_edit = QLineEdit()
        self.docs_edit.setPlaceholderText("https://… (optional)")
        self.docs_edit.textChanged.connect(self._update_button_states)
        form.addRow("Documentation:", self.docs_edit)

        self.summary_edit = QPlainTextEdit()
        self.summary_edit.setPlaceholderText("Short description (optional)")
        # Soft-wrap; this is a markdown table cell, not a paragraph
        # editor — line breaks get flattened to spaces on save anyway.
        self.summary_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.summary_edit.textChanged.connect(self._update_button_states)
        form.addRow("Summary:", self.summary_edit)
        return pane

    # ----------------------------------------------------------- list helpers
    def _refresh_list(self) -> None:
        """Rebuild list items from ``self._tools``. Blocks signals
        during the rebuild so ``currentRowChanged`` doesn't fire for
        every intermediate state."""
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for tool in self._tools:
            label = tool.name if tool.name.strip() else "(unnamed)"
            self.list_widget.addItem(QListWidgetItem(label))
        self.list_widget.blockSignals(False)

    def _set_form_enabled(self, enabled: bool) -> None:
        """Toggle the four form fields. Delete's enabled state is
        deliberately NOT touched here — it's owned by
        ``_update_button_states`` so a single source of truth drives
        action-button state from selection / dirty signals."""
        self.name_edit.setEnabled(enabled)
        self.main_edit.setEnabled(enabled)
        self.docs_edit.setEnabled(enabled)
        self.summary_edit.setEnabled(enabled)
        if not enabled:
            self.name_edit.clear()
            self.main_edit.clear()
            self.docs_edit.clear()
            self.summary_edit.clear()

    # ----------------------------------------------------------- form ↔ model
    def _capture_form_into(self, idx: int) -> None:
        """Write the current form fields back into ``self._tools[idx]``.
        Frozen dataclass → replace the list entry with a new instance."""
        if not (0 <= idx < len(self._tools)):
            return
        self._tools[idx] = AITool(
            name=self.name_edit.text().strip(),
            main_url=self.main_edit.text().strip(),
            docs_url=self.docs_edit.text().strip(),
            summary=self.summary_edit.toPlainText().strip(),
        )

    def _load_into_form(self, idx: int) -> None:
        tool = self._tools[idx]
        # Block textChanged on every field for the duration of the
        # load. Reasons (both load-bearing):
        #   1. ``_on_name_typed`` would mis-fire mid-load and rewrite
        #      the list-item label.
        #   2. ``_update_button_states`` would re-run after each
        #      field set, observing a transient mix of new-row +
        #      old-row content that looks "dirty" even when loading
        #      a clean row. The Save button would flicker on/off.
        # The caller (_on_row_changed) re-runs _update_button_states
        # explicitly after this returns, so the settled state is
        # captured exactly once.
        blockers = (self.name_edit, self.main_edit,
                    self.docs_edit, self.summary_edit)
        for w in blockers:
            w.blockSignals(True)
        try:
            self.name_edit.setText(tool.name)
            self.main_edit.setText(tool.main_url)
            self.docs_edit.setText(tool.docs_url)
            self.summary_edit.setPlainText(tool.summary)
        finally:
            for w in blockers:
                w.blockSignals(False)

    # ----------------------------------------------------------- signals
    def _on_row_changed(self, new_idx: int) -> None:
        """Selection changed. Capture the form into the previously
        displayed row (if any), then load the new row's data."""
        if self._current_idx is not None:
            self._capture_form_into(self._current_idx)
            # Live label sync — _capture_form_into may have changed
            # the name; reflect it on the previous list item.
            if 0 <= self._current_idx < self.list_widget.count():
                prev_item = self.list_widget.item(self._current_idx)
                if prev_item is not None:
                    prev_name = self._tools[self._current_idx].name.strip()
                    prev_item.setText(prev_name if prev_name else "(unnamed)")
        if 0 <= new_idx < len(self._tools):
            self._current_idx = new_idx
            self._load_into_form(new_idx)
            self._set_form_enabled(True)
        else:
            self._current_idx = None
            self._set_form_enabled(False)
        # Settled state — pick up the new selection (drives Delete)
        # and recompute dirty against the just-captured-form state
        # (drives Save).
        self._update_button_states()

    def _on_name_typed(self, text: str) -> None:
        """Mirror the Name field into the corresponding list item
        label as the user types — so the left pane reflects the
        rename immediately, without waiting for a row change."""
        if self._current_idx is None:
            return
        item = self.list_widget.item(self._current_idx)
        if item is None:
            return
        item.setText(text.strip() if text.strip() else "(unnamed)")

    def _on_add_clicked(self) -> None:
        # Capture before mutating the list — otherwise an unsaved
        # rename to the current row would be lost when the list
        # rebuilds and the current row index shifts.
        if self._current_idx is not None:
            self._capture_form_into(self._current_idx)
        new_tool = AITool(name="New Resource", main_url="", docs_url="",
                          summary="")
        self._tools.append(new_tool)
        self._refresh_list()
        new_idx = len(self._tools) - 1
        self.list_widget.setCurrentRow(new_idx)
        # Pre-select the placeholder name so typing immediately replaces it.
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _on_delete_clicked(self) -> None:
        if self._current_idx is None:
            return
        idx = self._current_idx
        name = self._tools[idx].name.strip() or "(unnamed)"
        ans = QMessageBox.question(
            self, "Delete Resource",
            f"Delete “{name}” from the resource list?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        del self._tools[idx]
        # Reset _current_idx BEFORE refreshing — _refresh_list calls
        # setCurrentRow below and we don't want _on_row_changed to
        # capture the about-to-be-stale form into a wrong index.
        self._current_idx = None
        self._refresh_list()
        if self._tools:
            next_idx = min(idx, len(self._tools) - 1)
            self.list_widget.setCurrentRow(next_idx)
        else:
            # No setCurrentRow → _on_row_changed won't fire → need
            # an explicit button-state refresh so Save reflects the
            # (now-dirty-pre-persist) state and Delete disables.
            self._set_form_enabled(False)
            self._update_button_states()
        # Persist the deletion immediately so the user doesn't have
        # to remember to click Save just to commit a delete — and
        # so Close won't prompt "Discard changes?" after a clean
        # delete. Validation runs against the post-delete list, so
        # a blanked name on the row being deleted doesn't block the
        # operation; only invalid edits to OTHER rows would surface.
        # On validation or write failure the in-memory deletion
        # stands but _snapshot is unchanged, so Close will still
        # prompt — correct, the user has an uncommitted deletion.
        # NOTE: no "Saved" confirmation here — the destructive
        # confirm dialog the user just clicked through (plus the
        # row visibly disappearing) is feedback enough; a second
        # modal would be two dialogs in a row for one action.
        if not self._validate_for_save():
            return
        self._persist_to_disk()

    def _on_save_clicked(self) -> None:
        if self._current_idx is not None:
            self._capture_form_into(self._current_idx)
        if not self._validate_for_save():
            return
        if self._persist_to_disk():
            self._show_save_confirmation()

    # ----------------------------------------------------------- save helpers
    def _validate_for_save(self) -> bool:
        """Walk ``self._tools`` and surface the first row with an
        empty required field. Returns False on the first failure
        (after jumping to the offender and showing a warning), True
        if every row passes.

        Required: Name + Main URL. Docs / Summary are optional —
        the view dialog already renders "(not listed)" for missing
        docs."""
        for idx, tool in enumerate(self._tools):
            if not tool.name:
                self._flag_invalid(idx, "Name is required.", self.name_edit)
                return False
            if not tool.main_url:
                self._flag_invalid(
                    idx, "Main Website URL is required.", self.main_edit)
                return False
        return True

    def _persist_to_disk(self) -> bool:
        """Save ``self._tools`` and refresh ``self._snapshot`` on
        success. Returns True if the file was written (clean dirty
        state), False if the write failed (error dialog already
        shown; ``_snapshot`` left untouched so the caller can keep
        track of uncommitted state).

        Snapshot refresh is the load-bearing line — without it,
        ``_is_dirty()`` would still compare against the
        originally-loaded list and the user would see a misleading
        "Discard changes?" prompt on Close. Same content-based-dirty
        discipline as ``EditorPanel`` (see CLAUDE.md)."""
        try:
            save_ai_tools(self._tools)
        except OSError as exc:
            _logger.exception("save_ai_tools failed")
            QMessageBox.critical(
                self, "Save Failed",
                f"Couldn't save the resource file:\n\n{exc}")
            return False
        self._snapshot = list(self._tools)
        # Snapshot now equals _tools → no longer dirty → Save
        # disables. Selection state is unchanged so Delete is
        # unaffected, but a single call covers both for free.
        self._update_button_states()
        return True

    def _show_save_confirmation(self) -> None:
        QMessageBox.information(
            self, "Saved",
            f"Resource changes saved ({len(self._tools)} entries).")

    def _update_button_states(self) -> None:
        """Refresh Save and Delete enabled states from the live
        model. Single source of truth — every site that can change
        selection, ``self._tools``, or ``self._snapshot`` either
        calls this directly or triggers a textChanged that does.

        * Save: enabled when content differs from the snapshot.
          ``_is_dirty()`` does the capture-then-compare against the
          in-flight form, so the button reacts to keystrokes as
          well as structural changes (Add / Delete).
        * Delete: enabled when a row is currently selected.
          ``_current_idx is None`` means either an empty list or
          a between-states moment in the action handlers — in both
          cases there's no row to delete."""
        self.delete_button.setEnabled(self._current_idx is not None)
        self.save_button.setEnabled(self._is_dirty())

    def _flag_invalid(self, idx: int, msg: str, focus_widget: QWidget) -> None:
        """Surface a validation failure: jump to the offending row,
        focus the offending field, show a message box. The row jump
        triggers _on_row_changed, which captures the (now-invalid)
        form into the current row first — that's fine, we're staying
        on the dialog and the user will fix it in place."""
        self.list_widget.setCurrentRow(idx)
        focus_widget.setFocus()
        QMessageBox.warning(self, "Cannot Save", msg)

    # ----------------------------------------------------------- cancel
    def _is_dirty(self) -> bool:
        """Compare the current list (after capturing the in-flight
        form edit) to the snapshot taken at open time. List equality
        on frozen dataclasses gives field-level comparison for free."""
        captured = list(self._tools)
        if self._current_idx is not None and 0 <= self._current_idx < len(captured):
            captured[self._current_idx] = AITool(
                name=self.name_edit.text().strip(),
                main_url=self.main_edit.text().strip(),
                docs_url=self.docs_edit.text().strip(),
                summary=self.summary_edit.toPlainText().strip(),
            )
        return captured != self._snapshot

    def _confirm_discard(self) -> bool:
        """Ask the user whether to throw away unsaved edits. Returns
        True if they chose Discard (caller should then revert),
        False if they chose Cancel (caller should leave state alone).

        Discard here does NOT close the dialog — it resets the
        in-memory list to the snapshot and leaves the user in the
        editor at the last-saved state. Closing the dialog requires
        a second Close click in the now-clean state. The wording
        makes that explicit so the button isn't surprising."""
        ans = QMessageBox.question(
            self, "Discard Changes?",
            "Discard your unsaved changes and return to the "
            "last-saved state? The editor stays open so you can "
            "keep working.",
            QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel)
        return ans == QMessageBox.Discard

    def _revert_to_snapshot(self) -> None:
        """Roll back ``self._tools`` to the snapshot, rebuild the
        list, and re-seed the form. Used by the Discard path so the
        user lands on a clean editor without a round-trip through
        the Resource menu.

        ``_current_idx`` is cleared *before* refreshing so the
        synthetic ``setCurrentRow`` below can't trigger
        ``_on_row_changed`` to capture the about-to-be-stale form
        into a wrong (pre-revert) index. Mirrors the same defence
        used in ``_on_delete_clicked``.

        Selection is restored to whatever row the user was on (e.g.
        edited "Cursor" then Discard → still on "Cursor"). The
        ``min(saved_idx, len-1)`` clamp covers the "+ Add then
        Discard" case where the saved index pointed at the
        just-evaporated new last row — fall back to the last
        surviving row so the editor doesn't yank focus to row 0
        purely as a side effect of the revert."""
        saved_idx = self._current_idx
        self._tools = list(self._snapshot)
        self._current_idx = None
        self._refresh_list()
        if not self._tools:
            self._set_form_enabled(False)
            self._update_button_states()
            return
        target = 0 if saved_idx is None else min(saved_idx, len(self._tools) - 1)
        self.list_widget.setCurrentRow(target)
        # _on_row_changed handles _current_idx assignment, form
        # reload, and the button-state refresh — no extra call needed.

    def _handle_cancel(self) -> None:
        """Close-button handler. Clean state → close; dirty state →
        prompt, and on Discard revert+stay (never close). The user
        explicitly asked for Close→Discard to leave them inside the
        editor rather than force a re-open from the Resource menu."""
        if not self._is_dirty():
            self.reject()
            return
        if self._confirm_discard():
            self._revert_to_snapshot()
        # Either button keeps the dialog open. Reject is intentionally
        # NOT called on the Discard branch — see _confirm_discard
        # docstring for the rationale.

    def closeEvent(self, event):  # noqa: N802 — Qt override naming
        """Route the window-close X through the same dirty-prompt
        path as the Close button. Symmetry matters: without it the
        user could lose changes via X (surprising), or — under the
        revert-and-stay semantics — get different exit behavior
        between X and Close (also surprising).

        Clean state → accept the close event. Dirty state → prompt;
        Discard reverts and ignores the event (stay open), Cancel
        ignores the event (stay open with edits intact). The only
        path that actually closes from a dirty state is "revert
        first, then X again." See ``_confirm_discard``."""
        if not self._is_dirty():
            event.accept()
            return
        if self._confirm_discard():
            self._revert_to_snapshot()
        event.ignore()
