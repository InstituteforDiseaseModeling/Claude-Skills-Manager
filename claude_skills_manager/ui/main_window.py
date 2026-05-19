"""Top-level QMainWindow: menus + toolbar + 3-pane splitter + status bar.

Owns the SkillScanner and routes signals between the three panels."""
from __future__ import annotations

import html
import logging
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import Qt, QSettings, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSplitter, QStatusBar, QToolBar, QToolButton, QWidget, QWidgetAction,
)

from ..logging_setup import log_file_path
from ..models import Skill, SkillType
from ..recycle import send_to_recycle_bin
from ..scanner import SkillScanner
from ..skill_settings import STATE_ON, write_override
from ._icons import close_icon, refresh_icon, search_icon
from ._styles import BUTTON_STYLE
from .about_dialog import AboutDialog
from .app_icon import app_icon, app_logo_pixmap, write_logo_ico
from .win32_taskbar import apply_window_appusermodel
from .editor_panel import EditorPanel
from .file_tree import FileTreePanel
from .image_dialog import ImageDialog
from .settings_dialog import SettingsDialog
from .skill_info_panel import SkillInfoPanel
from .skill_list import (
    STATE_GROUP_DISABLED, STATE_GROUP_ENABLED, SkillListPanel,
)
from .check_claude_dialog import CheckClaudeDialog
from .test_dialog import TestSkillDialog

_logger = logging.getLogger("main_window")

# Logo shown at the leftmost position of the toolbar. 24 logical px is
# Qt's default toolbar icon footprint — fits inside BUTTON_STYLE's 22px
# min-height button cap without forcing the toolbar taller. See §7.21
# for the design choice; the rendering itself comes from app_icon.
_TOOLBAR_LOGO_SIZE = 24

_ORG = "ClaudeSkillsManager"
_APP = "ClaudeSkillsManager"

# Quick allow-list of "definitely text"; anything else falls through to the
# null-byte sniff in `_is_text_file`.
_TEXT_EXTS = frozenset({
    ".md", ".markdown", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh", ".ps1",
    ".bat", ".html", ".htm", ".css", ".scss", ".xml", ".rst", ".log",
    ".env", ".gitignore", ".dockerignore", "",
})

# Extension-only allow-list for image preview. Qt's bundled image plugins
# cover all of these; we don't sniff content, so a file with the wrong
# extension would simply fail at QPixmap load (handled inside ImageDialog).
_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".webp", ".tiff", ".tif",
})


class _ViewMenuRow(QWidget):
    """Custom widget rendered inside a QWidgetAction in the View menu.

    Two click zones share one row:

    * **Title label (left)** — clicking anywhere outside the close
      button raises the corresponding Test Skill window. Handled by
      ``mousePressEvent`` on the row itself, which only fires when
      the click lands outside the close button (the button consumes
      its own clicks first).
    * **Close button (right)** — a small X tool button that closes
      the window. Distinct geometry so the user can target it
      independently of the title.

    Hover styling is restated here because ``QWidgetAction``'s
    embedded widget doesn't inherit the menu's native hover palette
    — by default the row looks dead. The selector targets the
    widget class via ``QWidget#viewMenuRow`` so the rule doesn't
    cascade to children (especially the QToolButton, which has
    its own ``autoRaise`` hover behavior)."""

    raise_requested = Signal()
    close_requested = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("viewMenuRow")
        # Wider min-width than a typical menu so titles like
        # "Test Skill — explain-github-workflow" don't elide.
        self.setMinimumWidth(380)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 4, 6, 4)
        layout.setSpacing(8)

        self._label = QLabel(title)
        self._label.setStyleSheet("color:#1a1a1a;")
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self._label, 1)

        self._close_btn = QToolButton()
        self._close_btn.setIcon(close_icon())
        self._close_btn.setAutoRaise(True)
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setToolTip("Close this Test Skill window")
        self._close_btn.clicked.connect(self.close_requested)
        layout.addWidget(self._close_btn)

        # Hover background mimicking a native menu item highlight.
        # Scoped via objectName so the rule doesn't leak to children;
        # the close button keeps its own QToolButton:hover style.
        self.setStyleSheet("""
            QWidget#viewMenuRow:hover {
                background: #e8eef9;
            }
            QToolButton:hover {
                background: #d6dff0;
                border-radius: 3px;
            }
        """)

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt naming
        # Clicks on the close button never reach here — the button
        # consumes them. So any click that DOES reach us is a click
        # on the title area, which means "raise."
        self.raise_requested.emit()
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, defer_initial_scan: bool = False) -> None:
        """Build the main window.

        ``defer_initial_scan``: when True, skip the implicit
        post-show ``QTimer.singleShot(0, self.refresh)`` so the
        caller (``main.py``) can drive the first scan synchronously
        while the splash is on screen. Refresh-button / F5 / context
        menu paths are unaffected — they call ``self.refresh()``
        directly and don't go through this flag."""
        super().__init__()
        self.setWindowTitle("Claude Skills Manager")
        # Per-window icon for title bar / Alt+Tab. NOT sufficient on its
        # own for the Windows taskbar — the shell resolves the taskbar
        # entry's icon via the AppUserModelID's registered .ico file,
        # which we attach in showEvent via apply_window_appusermodel.
        # See §7.22 for the full chain.
        self.setWindowIcon(app_icon())
        self.resize(1400, 880)

        self._scanner = SkillScanner()
        self._project_root: Path | None = None
        self._current_skill: Skill | None = None
        # Tracks whether the search box was empty as of the last
        # textChanged event. ``_on_search_changed`` resets the current
        # selection on empty <-> non-empty transitions only — not on
        # every keystroke while the search stays non-empty — so a dirty
        # file doesn't trigger the Discard prompt repeatedly while the
        # user types. See §7.27 for the full state-machine.
        self._search_was_empty = True
        # Guard so the (idempotent but allocating) Windows taskbar
        # binding only fires on the first show, not on every restore
        # from minimized state.
        self._taskbar_icon_bound = False
        # Per-skill open test dialogs (§7.34). Indexed by
        # ``Skill.path`` (the scanner's dedup key) so re-opening the
        # tester for a skill that already has a window raises the
        # existing one instead of creating a duplicate. Entries are
        # removed in ``_on_test_dialog_closed``, which the dialog
        # emits before destroying itself.
        self._test_dialogs: dict[Path, TestSkillDialog] = {}
        # Single-instance health-check dialog (§7.36). Distinct from
        # ``_test_dialogs`` because there's no per-skill axis — it's
        # a global "is `claude` working?" check, so one window at a
        # time is sufficient. Clicking the toolbar button a second
        # time raises the existing window instead of opening a duplicate.
        self._check_claude_dialog: CheckClaudeDialog | None = None

        self._build_ui()
        self._connect_signals()
        self._restore_settings()
        # Defer the initial scan so the window has a chance to paint
        # first — without this, ``refresh()`` runs inside ``__init__``
        # (before ``window.show()`` is called from ``main.py``), so the
        # busy indicator (§7.33) never gets a frame to render and the
        # user sees nothing while the scan is happening. ``singleShot(0)``
        # schedules the call for the next event-loop tick, after the
        # show event has fired.
        #
        # When ``defer_initial_scan`` is True the caller takes over —
        # main.py runs ``refresh()`` synchronously while the splash is
        # the visible surface, then shows this window post-scan. The
        # post-show paint argument doesn't apply in that path (this
        # window isn't shown yet) so the singleShot isn't needed.
        if not defer_initial_scan:
            QTimer.singleShot(0, self.refresh)

    def showEvent(self, event) -> None:  # noqa: N802 — Qt naming
        super().showEvent(event)
        # The HWND only exists after the window is first shown, and
        # SHGetPropertyStoreForWindow needs a valid HWND. Hooking
        # showEvent (rather than __init__) is the right ordering.
        if self._taskbar_icon_bound or sys.platform != "win32":
            return
        self._taskbar_icon_bound = True
        self._bind_windows_taskbar_icon()

    def _bind_windows_taskbar_icon(self) -> None:
        """Register the app's icon resource against the AppUserModelID
        on this window's HWND, so the Windows taskbar entry uses our
        custom logo rather than a blank/generic shell icon. Silent
        no-op on any failure — the in-app icon (title bar, toolbar,
        Alt+Tab) still works via the Qt setWindowIcon paths.

        The .ico file is written to the user's TEMP directory once
        per session. Windows reads the icon lazily, so the file must
        outlive the call — TEMP is fine because the file persists for
        the OS session and gets cleaned up by the OS later."""
        try:
            ico_path = Path(tempfile.gettempdir()) / "ClaudeSkillsManager_logo.ico"
            if not ico_path.exists():
                if not write_logo_ico(ico_path):
                    return
            apply_window_appusermodel(
                int(self.winId()),
                "ClaudeSkillsManager.ClaudeSkillsManager",
                ico_path,
            )
        except OSError:
            # Non-fatal — TEMP unwritable or similar. The in-app icon
            # surfaces still work; only the taskbar entry is affected.
            pass

    # ----------------------------------------------------------- UI assembly
    def _build_ui(self) -> None:
        # Panels first — the toolbar wires signals to skill_list, so it must
        # exist before _build_toolbar runs.
        self.skill_list = SkillListPanel()
        self.file_tree = FileTreePanel()
        self.skill_info = SkillInfoPanel()
        self.editor_panel = EditorPanel()

        self._build_menus()
        self.addToolBar(Qt.TopToolBarArea, self._build_toolbar())

        # Middle column is itself a vertical split: file tree on top, SKILL.md
        # metadata on bottom. Wrapping in a nested QSplitter keeps the outer
        # 3-column stretch factors untouched.
        middle = QSplitter(Qt.Vertical)
        middle.addWidget(self.file_tree)
        middle.addWidget(self.skill_info)
        middle.setStretchFactor(0, 3)
        middle.setStretchFactor(1, 1)
        middle.setSizes([520, 200])

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.skill_list)
        splitter.addWidget(middle)
        splitter.addWidget(self.editor_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 5)
        splitter.setSizes([280, 320, 800])
        self.setCentralWidget(splitter)

        self.setStatusBar(QStatusBar())
        # Indeterminate progress bar permanently mounted on the right
        # side of the status bar — hidden by default, shown only during
        # the busy state managed by ``_busy`` (§7.33). ``range=(0, 0)``
        # is Qt's switch into indeterminate / "marquee" mode; the bar
        # then animates a sliding fill independent of any actual percent
        # value, which is the honest signal for a scan whose duration
        # we can't predict in advance.
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setFixedSize(140, 14)
        self.busy_bar.hide()
        self.statusBar().addPermanentWidget(self.busy_bar)

    def _build_toolbar(self) -> QToolBar:
        bar = QToolBar("Main")
        # objectName is required by QMainWindow.saveState() — without it Qt
        # warns at every shutdown ("'objectName' not set for QToolBar"). The
        # constructor argument above is the window title for floating mode,
        # not an identifier; the two are distinct concepts.
        bar.setObjectName("MainToolBar")
        bar.setMovable(False)

        # Brand anchor — same composite logo used for the window icon,
        # rendered as an inline pixmap. Sits leftmost with a small left
        # padding so it doesn't crowd the window edge; right padding
        # creates breathing room before the "Project root:" label below.
        # No QToolBar separator after it — the padding is enough visual
        # break, and avoids stacking another vertical line next to the
        # title-bar boundary.
        logo_label = QLabel()
        logo_label.setPixmap(app_logo_pixmap(_TOOLBAR_LOGO_SIZE))
        logo_label.setStyleSheet("padding: 0 8px 0 6px;")

        # Rich-text label: bold "Project root:" prefix in dark-grey, the
        # path itself in a softer grey so the label and value have visual
        # hierarchy (label snaps the eye, value is for reading).
        self.root_label = QLabel()
        self.root_label.setTextFormat(Qt.RichText)
        self.root_label.setStyleSheet("padding:0 8px;")
        self._update_root_label()

        choose_root = QPushButton("Choose…")
        choose_root.setStyleSheet(BUTTON_STYLE)
        choose_root.clicked.connect(self.choose_project_root)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setIcon(refresh_icon())
        self.refresh_btn.setStyleSheet(BUTTON_STYLE)
        self.refresh_btn.setShortcut(QKeySequence.Refresh)  # F5
        self.refresh_btn.clicked.connect(self.refresh)
        # NOTE: "Test Skill…" and "Check Claude" buttons were intentionally
        # removed from the toolbar. The per-skill test runner lives in the
        # right-click context menu only; the CLI health check moved to
        # Help → Test Claude connection. Goal: keep `claude`-specific
        # affordances off the main toolbar so the surface stays focused on
        # browsing/editing.

        self.cb_global  = QCheckBox("Global")
        self.cb_project = QCheckBox("Project")
        self.cb_plugin  = QCheckBox("Plugin")
        self.cb_enabled  = QCheckBox("Enabled")
        self.cb_disabled = QCheckBox("Disabled")
        # Set initial state BEFORE connecting toggled — see §7.1: setChecked
        # fires toggled synchronously, so connecting first would invoke the
        # slot during construction before skill_list is ready (or before
        # restored settings can take effect).
        for cb in (self.cb_global, self.cb_project, self.cb_plugin,
                   self.cb_enabled, self.cb_disabled):
            cb.setChecked(True)
        self.cb_global.toggled.connect(
            lambda v: self.skill_list.set_type_enabled(SkillType.GLOBAL, v))
        self.cb_project.toggled.connect(
            lambda v: self.skill_list.set_type_enabled(SkillType.PROJECT, v))
        self.cb_plugin.toggled.connect(
            lambda v: self.skill_list.set_type_enabled(SkillType.PLUGIN, v))
        self.cb_enabled.toggled.connect(
            lambda v: self.skill_list.set_state_group_enabled(STATE_GROUP_ENABLED, v))
        self.cb_disabled.toggled.connect(
            lambda v: self.skill_list.set_state_group_enabled(STATE_GROUP_DISABLED, v))

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search skills…")
        self.search.setMaximumWidth(280)
        self.search.setClearButtonEnabled(True)
        # Leading magnifier icon — purely a visual marker. The search is
        # already live-as-you-type via textChanged below; the icon
        # signals "this is a search input" without requiring an action.
        # Tooltip provides a hover hint; the action is intentionally
        # left unconnected (clicking the icon is a no-op).
        search_action = QAction(search_icon(), "Search", self.search)
        self.search.addAction(search_action, QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self._on_search_changed)

        bar.addWidget(logo_label)
        bar.addWidget(self.root_label)
        bar.addWidget(choose_root)
        bar.addSeparator()
        bar.addWidget(self.refresh_btn)
        bar.addSeparator()
        bar.addWidget(_section_label("Type:"))
        bar.addWidget(self.cb_global)
        bar.addWidget(self.cb_project)
        bar.addWidget(self.cb_plugin)
        bar.addSeparator()
        bar.addWidget(_section_label("State:"))
        bar.addWidget(self.cb_enabled)
        bar.addWidget(self.cb_disabled)

        # Expanding spacer pushes the search box to the right edge.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)
        bar.addWidget(self.search)
        return bar

    def _update_root_label(self) -> None:
        """Re-render the toolbar's 'Project root:' label as rich text.
        Bold dark-grey prefix + softer grey path / "(none)" placeholder.
        ``html.escape`` guards against folder names containing characters
        that Qt's rich-text parser would interpret as markup."""
        if self._project_root:
            body = (f"<span style='color:#666;'>"
                    f"{html.escape(str(self._project_root))}</span>")
        else:
            body = "<span style='color:#999;'>(none)</span>"
        self.root_label.setText(
            f"<b style='color:#333;'>Project root:</b> {body}")

    def _connect_signals(self) -> None:
        self.skill_list.skill_selected.connect(self.on_skill_selected)
        self.skill_list.state_change_requested.connect(self._on_state_change_requested)
        # Context-menu "Test Skill…" → same entry point as the toolbar
        # button. The skill_list emits with a Skill, not the current
        # selection, so the user can right-click a different row than
        # whatever's selected and still test it directly.
        self.skill_list.test_skill_requested.connect(self.open_test_dialog)
        self.skill_list.delete_skill_requested.connect(
            self._on_delete_skill_requested)
        self.skill_info.state_change_requested.connect(self._on_state_change_requested)
        self.file_tree.file_activated.connect(self.on_file_activated)
        self.editor_panel.file_saved.connect(self._on_file_saved)

    # ------------------------------------------------------------------ slots
    def choose_project_root(self) -> None:
        start = str(self._project_root) if self._project_root else str(Path.cwd())
        chosen = QFileDialog.getExistingDirectory(self, "Choose project root", start)
        if not chosen:
            return
        # Confirm BEFORE mutating state. If the user has unsaved edits and
        # clicks Cancel on the discard prompt, refresh() would abort early
        # — leaving _project_root and the toolbar label set to the new
        # path while the skill list still reflected the old one. Asking
        # here means we either fully switch or don't switch at all.
        if not self.editor_panel.confirm_close():
            return
        self._project_root = Path(chosen)
        self._update_root_label()
        self.refresh()

    def _on_search_changed(self, text: str) -> None:
        """Search-box ``textChanged`` handler.

        The skill-list filter updates on every keystroke. Selection reset
        fires only on **empty <-> non-empty transitions**, not on every
        character typed:

        * **empty → non-empty** (user started searching): drop the
          selection so middle and right panels go blank. Per user spec
          §7.27: "since there is no skill is selected."
        * **non-empty → empty** (X click or backspace-all): same reset
          — the "start over" gesture mirroring app-start and Refresh.
        * **non-empty → non-empty** (refining the search): no-op. We
          already dropped on the first keystroke; re-running the reset
          on every keystroke would prompt the user about unsaved
          changes repeatedly, which is unusable.

        If the editor has unsaved changes, ``confirm_close()`` prompts.
        On Cancel we keep the selection and the new empty state. The
        user has to save/discard before the next transition triggers a
        new prompt — same handshake pattern as ``refresh()``."""
        self.skill_list.set_filter(text)
        empty_now = not text
        was_empty = self._search_was_empty
        self._search_was_empty = empty_now

        if was_empty == empty_now or self._current_skill is None:
            return
        if not self.editor_panel.confirm_close():
            return
        self._current_skill = None
        self.file_tree.clear()
        self.skill_info.clear()
        self.editor_panel.clear()
        self.skill_list.clear_selection()
        # No ``_busy`` here — the search-clear transition does no I/O,
        # so a full loading indicator would lie. A brief status-bar
        # flash is the honest acknowledgment (§7.33).
        message = "View cleared — start typing or pick a skill"
        self.statusBar().showMessage(message, 2500)

    @contextmanager
    def _busy(self, message: str):
        """Context manager that holds the UI in a "busy / loading" state
        for the duration of the block.

        On enter: shows the indeterminate progress bar in the status
        bar, swaps the cursor to ``Qt.WaitCursor``, disables the Refresh
        button (so the user can't trigger a second scan mid-way through
        the first), sets ``message`` in the status bar, and calls
        ``processEvents`` once so all of the above paint *before* the
        sync work below starts (without that flush, the indicator
        wouldn't appear until the event loop ran again — i.e., after
        the scan completed).

        On exit (including on exception): restores the cursor, hides
        the progress bar, re-enables the button. The status text is
        left for the caller to set on completion — that lets the caller
        choose between e.g. ``"Loaded N skills"`` and
        ``"Scan error: ..."`` without ``_busy`` having to know which
        path it's on.

        Limitation: the indeterminate animation only advances when the
        Qt event loop runs. During a sync scan the bar appears but
        doesn't visibly animate; the OS-level busy cursor is the
        animated cue. See §7.33 for the rationale on keeping the
        scanner sync (CLAUDE.md's "Qt-free scanner" rule)."""
        self.statusBar().showMessage(message)
        self.busy_bar.show()
        self.refresh_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            yield
        finally:
            QApplication.restoreOverrideCursor()
            self.busy_bar.hide()
            self.refresh_btn.setEnabled(True)

    def refresh(self) -> None:
        if not self.editor_panel.confirm_close():
            return
        # Wipe panels so stale state from before the rescan can't linger:
        # the previously-selected Skill object is about to be replaced.
        self._current_skill = None
        self.file_tree.clear()
        self.skill_info.clear()
        self.editor_panel.clear()

        with self._busy("Scanning skills…"):
            try:
                skills = self._scanner.scan_all(self._project_root)
            except Exception as e:  # last-ditch: keep the UI alive
                QMessageBox.warning(self, "Scan error", str(e))
                self.statusBar().showMessage(f"Scan error: {e}", 5000)
                return
            self.skill_list.set_skills(skills)
        self.statusBar().showMessage(f"Loaded {len(skills)} skills", 5000)

    def on_skill_selected(self, skill: Skill) -> None:
        if not self.editor_panel.confirm_close():
            # User cancelled the unsaved-changes prompt. The skill list's
            # visual selection has already moved to ``skill`` (Qt updates
            # the highlight synchronously, before _on_selection fires) —
            # restore it to whatever skill the editor is *actually*
            # showing, so the highlight matches the editor content. Same
            # pattern as on_file_activated for the file tree (§7.27): on
            # rejection, snap the view back to ground truth. If no skill
            # was previously selected, drop the highlight entirely rather
            # than leave it pinned to the rejected target.
            if self._current_skill is not None:
                if not self.skill_list.select_skill(self._current_skill):
                    self.skill_list.clear_selection()
            else:
                self.skill_list.clear_selection()
            return
        self._current_skill = skill
        self.file_tree.show_directory(skill.path)
        self.skill_info.show_skill(skill)
        self.editor_panel.show_skill(skill)
        self.statusBar().showMessage(f"{skill.type.value} • {skill.path}")

    def open_test_dialog(self, skill: Skill) -> None:
        """Open (or re-focus) the modeless test dialog for ``skill``.

        Per-skill instance via ``_test_dialogs`` map keyed on the
        absolute path: opening the dialog twice for the same skill
        raises the existing window rather than spawning a duplicate.
        Opening it for *different* skills creates parallel dialogs —
        user spec was "one per skill, multiple skills OK".

        The dialog manages its own lifetime
        (``WA_DeleteOnClose``); we only need to clear our map entry
        when it closes, which the ``closed`` signal handles."""
        existing = self._test_dialogs.get(skill.path)
        if existing is not None:
            # Route through _raise_dialog so the minimized-restore
            # idiom (clear WindowMinimized bit, then show + raise +
            # activate) lives in one place instead of being duplicated
            # at every "re-open" site.
            self._raise_dialog(existing)
            return
        dialog = TestSkillDialog(skill, parent=self)
        dialog.closed.connect(self._on_test_dialog_closed)
        self._test_dialogs[skill.path] = dialog
        dialog.show()
        self._rebuild_view_menu()

    def _on_test_dialog_closed(self, skill_path) -> None:
        """``TestSkillDialog.closed`` slot — drop the entry from the
        registry. The dialog itself is destroyed by Qt right after
        emitting this signal (``WA_DeleteOnClose`` triggers
        ``deleteLater`` from inside ``closeEvent``).

        ``skill_path`` arrives as ``object`` because Qt doesn't have a
        built-in registration for ``pathlib.Path`` signals; we coerce
        to ``Path`` only for the dict lookup."""
        self._test_dialogs.pop(Path(skill_path), None)
        self._rebuild_view_menu()

    def open_check_claude_dialog(self) -> None:
        """Open (or re-focus) the singleton health-check dialog (§7.36).
        Triggered from Help → Test Claude connection.

        Same raise-existing-or-create pattern as ``open_test_dialog``
        but without the per-skill axis — there's only one ``claude``
        install to test, so one dialog suffices."""
        if self._check_claude_dialog is not None:
            # Same restore-from-minimized handling as
            # open_test_dialog — kept centralized in _raise_dialog.
            self._raise_dialog(self._check_claude_dialog)
            return
        dlg = CheckClaudeDialog(parent=self)
        dlg.closed.connect(self._on_check_claude_dialog_closed)
        self._check_claude_dialog = dlg
        dlg.show()

    def _on_check_claude_dialog_closed(self) -> None:
        """Clear the registry slot when the dialog closes so the next
        click constructs a fresh instance (and re-runs the auto-test).
        Mirrors ``_on_test_dialog_closed`` for the per-skill case."""
        self._check_claude_dialog = None

    # ----------------------------------------------------------- menu bar
    def _build_menus(self) -> None:
        """Wire the menu bar: File / Help / View.

        Action shortcuts coexist with toolbar shortcuts via
        ``ApplicationShortcut`` context where useful. ``QAction`` with
        a shortcut auto-renders the chord in the menu's right gutter."""
        menubar = self.menuBar()

        # ---- File ----
        file_menu = menubar.addMenu("&File")
        act_refresh = QAction("&Refresh skills", self)
        act_refresh.setShortcut(QKeySequence.Refresh)  # F5
        act_refresh.triggered.connect(self.refresh)
        file_menu.addAction(act_refresh)
        file_menu.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # ---- View ----
        # Populated lazily from ``_test_dialogs`` via
        # ``_rebuild_view_menu``. Kept on ``self`` because we mutate
        # it on every dialog open / close. Constructed BEFORE Help so
        # the menu bar order is File / View / Help.
        self._view_menu = menubar.addMenu("&View")
        self._view_menu.aboutToShow.connect(self._rebuild_view_menu)
        self._rebuild_view_menu()

        # ---- Help ----
        help_menu = menubar.addMenu("&Help")
        act_check = QAction("&Test Claude connection", self)
        act_check.setShortcut(QKeySequence("Ctrl+T"))
        act_check.triggered.connect(self.open_check_claude_dialog)
        help_menu.addAction(act_check)
        act_open_logs = QAction("&Open log folder", self)
        act_open_logs.triggered.connect(self._open_log_folder)
        help_menu.addAction(act_open_logs)
        act_settings = QAction("&Settings…", self)
        act_settings.triggered.connect(self._open_settings_dialog)
        help_menu.addAction(act_settings)
        help_menu.addSeparator()
        act_about = QAction("&About", self)
        act_about.triggered.connect(self._open_about_dialog)
        help_menu.addAction(act_about)

    def _rebuild_view_menu(self) -> None:
        """Re-populate the View menu from the live ``_test_dialogs``
        map. Cheap (the map is small); called from every site that
        adds or removes an entry, plus ``aboutToShow`` as a backstop
        in case any path forgot to invalidate.

        Each row is a custom ``_ViewMenuRow`` widget wrapped in a
        ``QWidgetAction`` so the row can host two click zones: the
        title area (raises the window) and a close X button (closes
        it). The placeholder for the empty state stays a plain
        QAction — no per-row affordances to surface there."""
        if not hasattr(self, "_view_menu"):
            return
        self._view_menu.clear()
        # Sort by window title so the menu order is stable and
        # readable (alphabetical by skill name).
        entries = sorted(
            self._test_dialogs.items(),
            key=lambda kv: kv[1].windowTitle().lower(),
        )
        if not entries:
            placeholder = QAction(
                "(no open Skill Test windows)", self._view_menu)
            placeholder.setEnabled(False)
            self._view_menu.addAction(placeholder)
            return
        # Bulk "Close All" affordance at the top of the menu — hidden
        # when there's nothing open. Plain QAction (not a custom
        # _ViewMenuRow) so the visual hierarchy makes the bulk op
        # distinct from the per-dialog rows below. Italicized label
        # via QAction.setFont is overkill — the separator already
        # signals the boundary.
        act_close_all = QAction(
            f"Close All ({len(entries)})", self._view_menu)
        act_close_all.setToolTip(
            "Close every open Skill Test window. Each dialog's own "
            "cancel-on-close logic runs (in-flight `claude` runs are "
            "killed); no test data is persisted across closes.")
        act_close_all.triggered.connect(self._close_all_test_dialogs)
        self._view_menu.addAction(act_close_all)
        self._view_menu.addSeparator()
        for _, dialog in entries:
            row = _ViewMenuRow(dialog.windowTitle())
            # Default-argument captures the dialog by value at
            # connection time, sidestepping Python's late-binding
            # closure semantics in the for-loop.
            row.raise_requested.connect(
                lambda d=dialog: self._raise_dialog_from_menu(d))
            row.close_requested.connect(
                lambda d=dialog: self._close_dialog_from_menu(d))

            action = QWidgetAction(self._view_menu)
            action.setDefaultWidget(row)
            self._view_menu.addAction(action)

    def _raise_dialog_from_menu(self, dialog) -> None:
        """Raise the dialog AND dismiss the menu. QWidgetAction
        widgets don't auto-close their parent menu on click, so we
        close it explicitly — otherwise the menu lingers above the
        window the user just asked to see."""
        self._view_menu.close()
        self._raise_dialog(dialog)

    def _close_all_test_dialogs(self) -> None:
        """Close every open Test Skill dialog. Triggered from the
        View menu's "Close All" entry.

        Snapshot the values list before iterating — each
        ``dialog.close()`` triggers ``WA_DeleteOnClose`` →
        ``closed`` signal → ``_on_test_dialog_closed`` → pops the
        entry from ``self._test_dialogs`` and rebuilds the menu.
        Iterating the live dict here would raise
        ``RuntimeError: dictionary changed size during iteration``.

        No confirmation: each dialog's own ``closeEvent`` already
        cancels in-flight ``claude`` runs and tears down gracefully,
        and there's no persistent state to lose (Continue-conversation
        session ids live on disk in Claude's own state, not in the
        dialog). The bulk gesture maps 1:1 onto clicking X on each
        dialog individually."""
        self._view_menu.close()
        for dialog in list(self._test_dialogs.values()):
            dialog.close()

    def _close_dialog_from_menu(self, dialog) -> None:
        """Close the dialog AND dismiss the menu. ``dialog.close()``
        triggers ``WA_DeleteOnClose`` → emits ``closed`` →
        ``_on_test_dialog_closed`` → ``_rebuild_view_menu``, so the
        row will be gone the next time the user opens View. No
        manual map maintenance needed here."""
        self._view_menu.close()
        dialog.close()

    @staticmethod
    def _raise_dialog(dialog) -> None:
        """Bring a child dialog forward — restoring it from a minimized
        state if necessary.

        ``show()`` and ``raise_()`` alone don't restore a minimized
        window on Windows: a minimized window is still ``isVisible()``,
        and the minimized state lives in ``windowState()`` rather than
        the visibility flag. To un-minimize we clear the
        ``WindowMinimized`` bit explicitly. Bit-mask manipulation
        (rather than ``showNormal()``) preserves any
        ``WindowMaximized`` bit, so a minimize-from-maximized restores
        back to maximized — matching the OS taskbar's behaviour.
        ``WindowActive`` is OR-ed in to cue the WM to give the window
        focus on restore."""
        dialog.setWindowState(
            (dialog.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_settings_dialog(self) -> None:
        SettingsDialog(self).exec()

    def _open_about_dialog(self) -> None:
        AboutDialog(self).exec()

    def _open_log_folder(self) -> None:
        """Reveal the directory containing the log file. Uses
        ``QDesktopServices.openUrl`` so the OS opens its native file
        manager (Explorer on Windows, Finder on macOS, default file
        manager on Linux) — much friendlier than spawning a Shell
        process directly."""
        log_path = log_file_path()
        target = log_path.parent
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        if not opened:
            QMessageBox.information(
                self, "Open log folder",
                f"Log file: {log_path}\n\nCouldn't open the folder "
                f"automatically. The path above is the location.")

    def on_file_activated(self, path: Path) -> None:
        # Image files open in a dedicated modal viewer — Editor tab can't
        # render them, and showing a "cannot open" message for skill assets
        # like PNG/SVG diagrams is unhelpful.
        if _is_image_file(path):
            ImageDialog(path, self).exec()
            return
        if not _is_text_file(path):
            QMessageBox.information(self, "Cannot open",
                                    f"{path.name} doesn't look like a text file.")
            return
        if not self.editor_panel.open_file(path):
            # The editor refused the open (most commonly: user clicked
            # Cancel on the unsaved-changes prompt). The tree's visual
            # selection has already moved to ``path`` from the click —
            # restore it to whatever file the editor is *actually*
            # showing, so the highlight matches the editor content.
            current = self.editor_panel.current_path()
            if current is not None:
                self.file_tree.select_path(current)

    def _on_file_saved(self, path: Path) -> None:
        self.statusBar().showMessage(f"Saved {path}", 4000)
        # If the user just edited the SKILL.md of the active skill,
        # re-scan so name/description in the left list stay in sync.
        if (self._current_skill is not None
                and self._current_skill.skill_md_path == path):
            self.refresh()

    def _on_state_change_requested(self, skill: Skill, new_state: str) -> None:
        """Persist a skill enable/disable toggle and refresh the affected
        UI rows in place — no full rescan needed for a one-skill change."""
        scope_dir = self._scope_dir_for(skill)
        if scope_dir is None:
            QMessageBox.warning(
                self, "Cannot toggle",
                "Plugin skills can't be toggled individually — use /plugin in "
                "Claude Code to disable the whole plugin.")
            return
        # Write None for "on" so the entry is removed (absent == default = on),
        # keeping settings.local.json minimal as the /skills menu does.
        to_write = None if new_state == STATE_ON else new_state
        try:
            write_override(scope_dir, skill.name, to_write)
        except (ValueError, OSError) as e:
            QMessageBox.warning(
                self, "Couldn't update settings",
                f"Failed to update {scope_dir / 'settings.local.json'}:\n\n{e}")
            return
        skill.state = new_state
        self.skill_list.refresh_state(skill)
        if (self._current_skill is not None
                and self._current_skill.path == skill.path):
            self.skill_info.show_skill(skill)
        word = "Enabled" if new_state == STATE_ON else "Disabled"
        self.statusBar().showMessage(f"{word} {skill.name}", 4000)

    def _on_delete_skill_requested(self, skill: Skill) -> None:
        """Two-step confirmation, then soft-delete the skill folder to
        the OS Recycle Bin and trigger a rescan.

        Plugin skills are rejected defensively (the menu hides the
        entry, but a future code path that emits the signal directly
        shouldn't be able to slip a plugin delete through). The two
        dialogs are deliberately different in tone:

        * The first is a standard Question with Yes/No — fast to dismiss
          if the user mis-clicked.
        * The second is a Warning with explicit verb-noun buttons
          ("Move to Recycle Bin" / "Cancel"), default Cancel — a small
          guard against muscle-memory double-Yes.

        Both have ``No`` / ``Cancel`` as the default button so the safe
        outcome happens if the user hits Enter without reading."""
        if skill.type == SkillType.PLUGIN:
            # Should never reach here (menu hides the entry), but a
            # signal-level guard is cheap and defends against a future
            # caller wiring the signal up differently.
            QMessageBox.warning(
                self, "Cannot delete",
                "Plugin skills can't be deleted from this GUI — uninstall "
                "the plugin via /plugin in Claude Code.")
            return

        # File count is informational — gives the user a sense of how
        # much is about to move. rglob is cheap for a skill folder
        # (typically tens of files, not thousands).
        try:
            file_count = sum(1 for _ in skill.path.rglob("*") if _.is_file())
        except OSError:
            file_count = -1  # unknown — don't block the flow on a stat error
        files_blurb = (
            f"{file_count} file{'s' if file_count != 1 else ''}"
            if file_count >= 0 else "the folder contents")

        # ----- Confirmation 1: standard Yes/No -----
        first = QMessageBox.question(
            self,
            "Delete skill?",
            f"Move skill <b>{skill.name}</b> to the Recycle Bin?<br><br>"
            f"<span style='color:#555;'>Location:</span> "
            f"<code>{skill.path}</code><br>"
            f"<span style='color:#555;'>Contents:</span> {files_blurb}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if first != QMessageBox.Yes:
            return

        # ----- Confirmation 2: Warning, verb-noun buttons -----
        second = QMessageBox(self)
        second.setIcon(QMessageBox.Warning)
        second.setWindowTitle("Are you sure?")
        second.setText(
            f"This will move <b>{skill.name}</b> ({files_blurb}) "
            "to the Recycle Bin.")
        second.setInformativeText(
            "You can restore the skill from the Recycle Bin until "
            "you empty it.")
        recycle_btn = second.addButton(
            "Move to Recycle Bin", QMessageBox.AcceptRole)
        cancel_btn = second.addButton(
            "Cancel", QMessageBox.RejectRole)
        second.setDefaultButton(cancel_btn)
        second.exec()
        if second.clickedButton() is not recycle_btn:
            return

        # ----- Execute -----
        try:
            send_to_recycle_bin(skill.path)
        except (OSError, FileNotFoundError,
                NotImplementedError) as exc:
            QMessageBox.critical(
                self, "Couldn't delete skill",
                f"Failed to move <b>{skill.name}</b> to the Recycle Bin:"
                f"<br><br><code>{exc}</code>")
            return

        self.statusBar().showMessage(
            f"Moved {skill.name} to Recycle Bin", 4000)
        # Full refresh — the path is gone, so any panel still pointing
        # at it (file tree, editor buffer, skill info) gets cleared by
        # MainWindow.refresh()'s per-panel clear() orchestration.
        self.refresh()

    def _scope_dir_for(self, skill: Skill) -> Path | None:
        """Return the .claude directory whose settings.local.json controls
        this skill's overrides, or None for plugin skills (not toggleable)."""
        if skill.type == SkillType.PLUGIN:
            return None
        # Skill paths look like <scope>/.claude/skills/<name>/, so parents[1]
        # is the .claude folder for both Global and Project skills.
        try:
            return skill.path.parents[1]
        except IndexError:
            return None

    # --------------------------------------------------------- settings & exit
    def closeEvent(self, event) -> None:  # noqa: N802
        if not self.editor_panel.confirm_close():
            event.ignore()
            return
        self._save_settings()
        super().closeEvent(event)

    def _save_settings(self) -> None:
        s = QSettings(_ORG, _APP)
        s.setValue("project_root", str(self._project_root) if self._project_root else "")
        s.setValue("show_global",   self.cb_global.isChecked())
        s.setValue("show_project",  self.cb_project.isChecked())
        s.setValue("show_plugin",   self.cb_plugin.isChecked())
        s.setValue("show_enabled",  self.cb_enabled.isChecked())
        s.setValue("show_disabled", self.cb_disabled.isChecked())
        s.setValue("geometry",      self.saveGeometry())
        s.setValue("state",         self.saveState())

    def _restore_settings(self) -> None:
        s = QSettings(_ORG, _APP)
        root = s.value("project_root") or ""
        if root and Path(root).exists():
            self._project_root = Path(root)
            self._update_root_label()
        for cb, key in (
            (self.cb_global,   "show_global"),
            (self.cb_project,  "show_project"),
            (self.cb_plugin,   "show_plugin"),
            (self.cb_enabled,  "show_enabled"),
            (self.cb_disabled, "show_disabled"),
        ):
            v = s.value(key)
            if v is not None:
                cb.setChecked(_truthy(v))
        geom = s.value("geometry")
        if geom is not None:
            self.restoreGeometry(geom)
        state = s.value("state")
        if state is not None:
            self.restoreState(state)


# ----------------------------------------------------------------------- helpers
def _section_label(text: str) -> QLabel:
    """Bold dark-grey label used to mark toolbar sections (Type:, State:).
    Rendered via rich text so the colour is decoupled from any global
    palette — the label keeps its emphasis even if the user's Qt theme
    repaints surrounding widgets."""
    lbl = QLabel(f"<b style='color:#333;'>{html.escape(text)}</b>")
    lbl.setTextFormat(Qt.RichText)
    lbl.setStyleSheet("padding:0 8px;")
    return lbl


def _truthy(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).lower() in {"true", "1", "yes", "on"}


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in _TEXT_EXTS:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(2048)
    except OSError:
        return False
    return b"\x00" not in chunk


def _is_image_file(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_EXTS
