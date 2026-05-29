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
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QSizePolicy, QSplitter, QStatusBar, QToolBar, QToolButton, QWidget,
    QWidgetAction,
)

from ..logging_setup import log_file_path
from ..models import Skill, SkillType
from ..recycle import send_to_recycle_bin
from ..scanner import SkillScanner
from ..skill_relocate import (
    PartialMoveError, RelocationCollision, copy_skill, move_skill,
    resolve_destination,
)
from ..skill_settings import STATE_ON, STATE_PLUGIN_OFF, write_override
from ._icons import close_icon, refresh_icon, search_icon
from ._styles import BUTTON_STYLE
from ..ai_tools import AITool, load_ai_tools
from .about_dialog import AboutDialog
from .ai_tool_dialog import AIToolDialog
from .app_icon import app_icon, app_logo_pixmap, write_logo_ico
from .edit_resource_dialog import EditResourceDialog
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
from .new_skill_dialog import NewSkillDialog
from .test_dialog import TestSkillDialog

_logger = logging.getLogger("main_window")

# Logo shown at the leftmost position of the toolbar. 24 logical px is
# Qt's default toolbar icon footprint — fits inside BUTTON_STYLE's 22px
# min-height button cap without forcing the toolbar taller. See §7.21
# for the design choice; the rendering itself comes from app_icon.
_TOOLBAR_LOGO_SIZE = 24

_ORG = "ClaudeSkillsManager"
_APP = "ClaudeSkillsManager"

# Resource-menu sort modes, persisted under the QSettings key
# ``resource_sort``. ``default`` preserves the row order of
# ``ai_tools.md`` as parsed (so users can hand-curate ordering via
# Edit Resource…); the other two are pure alphabetical on the tool
# name, case-insensitive.
_RESOURCE_SORT_DEFAULT = "default"
_RESOURCE_SORT_ASC = "asc"
_RESOURCE_SORT_DESC = "desc"
_RESOURCE_SORT_VALID = (
    _RESOURCE_SORT_DEFAULT, _RESOURCE_SORT_ASC, _RESOURCE_SORT_DESC,
)

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


class _WindowMenuRow(QWidget):
    """Custom widget rendered inside a QWidgetAction in the Window menu.

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
    widget class via ``QWidget#windowMenuRow`` so the rule doesn't
    cascade to children (especially the QToolButton, which has
    its own ``autoRaise`` hover behavior)."""

    raise_requested = Signal()
    close_requested = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("windowMenuRow")
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
            QWidget#windowMenuRow:hover {
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


class _StayOpenMenu(QMenu):
    """A QMenu that does NOT collapse the menu chain when one of its
    *checkable* actions is activated. Used for the Resource → Sort by
    submenu so the user can flip between sort modes without the parent
    Resource menu closing under them on every click.

    Qt's default ``QMenu.mouseReleaseEvent`` and ``keyPressEvent``
    activate the action AND close the entire menu chain back to the
    menu bar — both behaviours live in the same code path. The
    canonical "stay open" idiom is to override those two events,
    call ``action.trigger()`` manually (which emits ``triggered`` and
    toggles the checked state), and ``return`` without chaining to
    ``super()``. The close-chain logic in ``super()`` never runs, so
    the menu stays open; the activation logic runs explicitly via
    ``trigger()``. The QActionGroup that owns the checkable actions
    repaints itself on the state flip, so the new check-mark gutter
    glyph appears in place without further work.

    Non-checkable actions fall through to ``super()`` unchanged —
    callers can mix transient actions into the same menu and they
    behave normally (activate + close). Today the Sort submenu only
    holds the three radio actions, but keeping the fall-through is
    cheap defence against future additions."""

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 — Qt naming
        action = self.activeAction()
        if action is not None and action.isCheckable() and action.isEnabled():
            action.trigger()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 — Qt naming
        # Keyboard parity with the mouse-release path. Without this,
        # arrow-key navigation followed by Space / Enter would still
        # close the menu chain because Qt's default keyPressEvent
        # is where the activation+close path lives for keys.
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            action = self.activeAction()
            if action is not None and action.isCheckable() and action.isEnabled():
                action.trigger()
                event.accept()
                return
        super().keyPressEvent(event)


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

        # NOTE: "New Skill" toolbar button was intentionally removed.
        # Skill creation is reachable two ways now:
        #   * File menu → "&New Skill…" (Ctrl+N) — global affordance.
        #   * Right-click on the "Global" or "Project" group header
        #     in the left skill list — contextual, prefills the
        #     Type radio with the section clicked.
        # The toolbar button was redundant given those two paths and
        # kept a less-contextual affordance taking up toolbar real
        # estate. See §7.58 for the iteration history.
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
            lambda v: self._on_type_filter_toggled(SkillType.GLOBAL, v))
        self.cb_project.toggled.connect(
            lambda v: self._on_type_filter_toggled(SkillType.PROJECT, v))
        self.cb_plugin.toggled.connect(
            lambda v: self._on_type_filter_toggled(SkillType.PLUGIN, v))
        self.cb_enabled.toggled.connect(
            lambda v: self._on_state_filter_toggled(STATE_GROUP_ENABLED, v))
        self.cb_disabled.toggled.connect(
            lambda v: self._on_state_filter_toggled(STATE_GROUP_DISABLED, v))

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

        # Expanding spacer pushes Type/State filters AND the search
        # box together to the right edge. Single spacer per toolbar:
        # ``QSizePolicy.Expanding`` is a one-shot push that absorbs
        # all free horizontal space at one point in the
        # left-to-right order, so everything declared after it
        # right-aligns as a group. Earlier iterations placed the
        # spacer between State and search to right-align only the
        # search; moving it ahead of Type pulls the filters along
        # too, matching the "filters live next to the search" UX
        # the user asked for.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)

        bar.addWidget(_section_label("Type:"))
        bar.addWidget(self.cb_global)
        bar.addWidget(self.cb_project)
        bar.addWidget(self.cb_plugin)
        bar.addSeparator()
        bar.addWidget(_section_label("State:"))
        bar.addWidget(self.cb_enabled)
        bar.addWidget(self.cb_disabled)
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
        # Group-header right-click "New … Skill…" → open the
        # creation dialog with the section's type preselected. Same
        # signal-up / MainWindow-handles-effect pattern as Test and
        # Delete above. The lambda captures the SkillType payload as
        # the dialog's ``initial_type`` so right-clicking Global
        # opens with Global selected, etc.
        self.skill_list.new_skill_requested.connect(
            lambda t: self.open_new_skill_dialog(initial_type=t))
        # Drag-drop a Project skill onto the Global header (§7.66).
        # Receiver pops a Copy / Move / Cancel confirmation dialog and
        # dispatches to ``skill_relocate``. Plugin source / target are
        # rejected at the UI layer (no drag flag / no drop flag) and at
        # the domain layer (ValueError in skill_relocate); the receiver
        # is the user-facing surface for any errors that slip through.
        self.skill_list.skill_drop_requested.connect(self._on_skill_drop)
        self.skill_info.state_change_requested.connect(self._on_state_change_requested)
        self.file_tree.file_activated.connect(self.on_file_activated)
        self.editor_panel.file_saved.connect(self._on_file_saved)
        self._install_shortcuts()

    def _install_shortcuts(self) -> None:
        """Window-scoped hotkeys that act on the currently selected
        skill in the left list (§7.69).

        These QActions are headless — not attached to any menu — but
        ``self.addAction(...)`` parents them on the main window so the
        WindowShortcut context fires whenever this window is active and
        a skill is selected. The skill-list right-click menu shows the
        chord in its right gutter via a ``\\t``-suffixed label rather
        than a real ``setShortcut`` on the transient menu QAction —
        that avoids the "ambiguous shortcut" warning Qt emits when two
        QActions in the same context have the same key sequence (one
        persistent here, one transient on the menu).

        Both handlers no-op when no skill is selected; we *don't*
        disable the QAction itself, because enabling/disabling on
        every selection change would require wiring an extra signal,
        and the no-skill case is rare and silent (no toast, no error
        — pressing Ctrl+T on an empty selection is a user-error the
        UI doesn't need to call out)."""
        self._test_skill_action = QAction("Test Skill", self)
        self._test_skill_action.setShortcut(QKeySequence("Ctrl+T"))
        self._test_skill_action.setShortcutContext(Qt.WindowShortcut)
        self._test_skill_action.triggered.connect(
            self._on_test_skill_shortcut)
        self.addAction(self._test_skill_action)

        self._open_folder_action = QAction(
            "Open Folder in Explorer", self)
        self._open_folder_action.setShortcut(QKeySequence("Ctrl+E"))
        self._open_folder_action.setShortcutContext(Qt.WindowShortcut)
        self._open_folder_action.triggered.connect(
            self._on_open_folder_shortcut)
        self.addAction(self._open_folder_action)

        # Ctrl+D / Del — Delete Skill… (Global/Project only, mirrors
        # the right-click menu which hides the entry entirely for
        # Plugin skills per §7.58 / the per-type mutation rule). Same
        # window-scoped, no-op-on-empty pattern as Ctrl+T / Ctrl+E.
        # The real double-confirmation flow lives in
        # ``_on_delete_skill_requested`` — kept centralized there so
        # right-click, Ctrl+D, and Del share one code path.
        #
        # Two alternates via ``setShortcuts([...])`` (plural) — Qt's
        # shortcut engine matches either. The bare Delete key is safe
        # here because Qt routes key events to the focus widget first:
        # ``QLineEdit`` (search) and ``QTextEdit`` (editor) consume
        # Delete for character erasure before shortcut matching, so
        # this binding only fires when focus is on a widget that
        # doesn't swallow the key (skill list, file tree, the window
        # chrome itself). The two-step confirmation flow is a second
        # safety net for the file-tree-focused edge case.
        self._delete_skill_action = QAction("Delete Skill…", self)
        self._delete_skill_action.setShortcuts([
            QKeySequence("Ctrl+D"),
            QKeySequence("Del"),
        ])
        self._delete_skill_action.setShortcutContext(Qt.WindowShortcut)
        self._delete_skill_action.triggered.connect(
            self._on_delete_skill_shortcut)
        self.addAction(self._delete_skill_action)

    def _on_test_skill_shortcut(self) -> None:
        """Ctrl+T handler — opens the Test Skill dialog for the
        currently selected skill. No-op when nothing is selected."""
        if self._current_skill is None:
            return
        self.open_test_dialog(self._current_skill)

    def _on_delete_skill_shortcut(self) -> None:
        """Ctrl+D handler — initiates the Delete Skill… flow for the
        currently selected skill. Silent no-op when nothing is
        selected, or when the selection is a Plugin skill (Plugin
        skills can't be deleted from the GUI — same gating the
        right-click menu enforces by hiding the entry). The actual
        double-confirmation + Recycle-Bin work is delegated to
        ``_on_delete_skill_requested`` so right-click and Ctrl+D
        funnel through one code path."""
        if self._current_skill is None:
            return
        if self._current_skill.type == SkillType.PLUGIN:
            return
        self._on_delete_skill_requested(self._current_skill)

    def _on_open_folder_shortcut(self) -> None:
        """Ctrl+E handler — reveals the currently selected skill's
        folder in the system file manager. No-op when nothing is
        selected. Same backend as the context-menu 'Open Folder in
        Explorer' entry (``QDesktopServices.openUrl`` on a
        ``file://`` URL), kept inline rather than routed through a
        signal because it's a pure local side-effect with no
        MainWindow state to mutate."""
        if self._current_skill is None:
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._current_skill.path)))

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

        Visibility-driven: applies the new filter, then delegates to
        the shared ``_reset_view_after_filter_change`` — same
        contract as the Type/State checkbox toggles. If the current
        skill is still visible under the new query the highlight
        gets re-asserted and the panels stay populated; if it's
        filtered out, the panels clear.

        Per-keystroke ``confirm_close`` prompts are naturally
        suppressed because the clear branch only runs when
        ``select_skill`` returns False — i.e. visibility actually
        changed. Typing within a stable result set just re-asserts
        the same selection without entering the prompt path."""
        self.skill_list.set_filter(text)
        self._reset_view_after_filter_change()

    def _on_type_filter_toggled(self, type_: SkillType, enabled: bool) -> None:
        """Toolbar Type checkbox handler.

        Wraps ``skill_list.set_type_enabled`` with the same
        middle/right reset gesture the search box uses on a context
        change (§7.27): after the rebuild drops the tree's visual
        highlight, the populated middle/right panels would otherwise
        show a skill the user can no longer see selected. Symmetric
        with ``_on_state_filter_toggled``; both delegate the reset
        to ``_reset_view_after_filter_change``."""
        self.skill_list.set_type_enabled(type_, enabled)
        self._reset_view_after_filter_change()

    def _on_state_filter_toggled(self, group: str, enabled: bool) -> None:
        """Toolbar State checkbox handler. Mirrors
        ``_on_type_filter_toggled`` for the Enabled / Disabled
        State group."""
        self.skill_list.set_state_group_enabled(group, enabled)
        self._reset_view_after_filter_change()

    def _reset_view_after_filter_change(self) -> None:
        """Reconcile the middle/right panels with the rebuilt skill list.

        Visibility-driven: shared by ``_on_search_changed`` and the
        Type/State toggle handlers. Three branches:

        * **No skill currently selected** — nothing to reconcile.
          Short-circuits the synchronous ``toggled`` emissions
          during ``_restore_settings`` (called after signal wiring
          with no skill selected yet).
        * **Current skill still visible** — re-assert the tree
          highlight (``_rebuild`` dropped it along with the old
          items) and leave the middle/right panels untouched. This
          is the common case: filter changes that don't affect
          what's selected don't disturb the user's editing context.
        * **Current skill filtered out** — clear panels and
          selection so the right side doesn't display a skill the
          user can no longer see in the left list. Unsaved edits
          surface a ``confirm_close`` prompt; cancelling it leaves
          the panels populated with no left-side highlight (same
          shape as the rejected-switch path in
          ``on_skill_selected``)."""
        if self._current_skill is None:
            return
        if self.skill_list.select_skill(self._current_skill):
            return
        if not self.editor_panel.confirm_close():
            return
        self._current_skill = None
        self.file_tree.clear()
        self.skill_info.clear()
        self.editor_panel.clear()
        self.skill_list.clear_selection()
        self.statusBar().showMessage(
            "Selected skill no longer matches — view cleared", 2500)

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

    def refresh(self, *, on_progress=None) -> None:
        """Re-scan all three skill sources and repopulate the left list.

        ``on_progress`` (keyword-only) is forwarded to
        ``SkillScanner.scan_all`` for the launch-time splash pump
        (see §7.62). Only ``main.py`` passes it — F5 / Refresh
        button / Choose-root / context-menu paths leave it at
        ``None`` so the status-bar busy bar's behavior is unchanged
        (pumping events while MainWindow is visible would surface
        queued user input mid-scan, which the sync-refresh contract
        doesn't handle). The startup path is special because the
        main window isn't visible yet — only the splash is on
        screen, so pumping events does nothing user-facing besides
        advancing the marquee."""
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
                skills = self._scanner.scan_all(
                    self._project_root, on_progress=on_progress)
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
        self._rebuild_window_menu()

    def _on_test_dialog_closed(self, skill_path) -> None:
        """``TestSkillDialog.closed`` slot — drop the entry from the
        registry. The dialog itself is destroyed by Qt right after
        emitting this signal (``WA_DeleteOnClose`` triggers
        ``deleteLater`` from inside ``closeEvent``).

        ``skill_path`` arrives as ``object`` because Qt doesn't have a
        built-in registration for ``pathlib.Path`` signals; we coerce
        to ``Path`` only for the dict lookup."""
        self._test_dialogs.pop(Path(skill_path), None)
        self._rebuild_window_menu()

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

    def open_new_skill_dialog(
        self, *, initial_type: SkillType | None = None,
    ) -> None:
        """Open the modal "New Skill" creation form. On accept,
        refresh the skill list and select the new skill (which
        routes through ``on_skill_selected`` to populate the file
        tree and Description tab).

        Per-type gating lives inside the dialog: Plugin is
        permanently disabled. Global and Project are both always
        enabled — the dialog has its own editable Project root
        field (prefilled with ``self._project_root`` when set),
        so the user can drop a skill into any folder without
        bouncing back to the main window first.

        ``initial_type`` (keyword-only) preselects the Type radio
        when provided — used by the group-header right-click flow
        in the left skill list. ``None`` (the default, used by the
        File menu / Ctrl+N path) falls through to the dialog's own
        default-Type logic."""
        dialog = NewSkillDialog(
            self,
            project_root=self._project_root,
            initial_type=initial_type,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        new_path = dialog.created_skill_md_path
        if new_path is None:
            # Dialog accepted without setting the path — defensive
            # branch; should never happen because _on_create_clicked
            # only calls accept() after setting created_skill_md_path.
            return

        self.refresh()
        # After refresh, the new skill is in the scanner's result.
        # Find by skill_md_path so we pick the right entry even if a
        # case-collision somehow slipped through (it shouldn't, but
        # belt + suspenders against future case-sensitivity bugs).
        new_skill = self._find_skill_by_md_path(new_path)
        if new_skill is None:
            # The file exists on disk but didn't appear in the scan
            # result. Two common causes:
            #   (a) A Type/State filter checkbox is hiding the row.
            #   (b) The user created a Project skill under a root
            #       different from the MainWindow's _project_root,
            #       so the scanner walked a different folder. The
            #       dialog supports per-instance roots (different
            #       from the main window's value) precisely for
            #       this use case, so it's a real-world flow, not
            #       a corner case.
            #
            # ``QMessageBox.information`` rather than a status-bar
            # flash because the user expected a visible new row in
            # the list and *didn't* get one — they need to act
            # (adjust filters or switch root) before the new skill
            # surfaces. A modal acknowledgment makes that requirement
            # impossible to miss.
            QMessageBox.information(
                self,
                "Skill created",
                f"Skill <b>{new_path.parent.name}</b> was "
                f"successfully created at:<br><br>"
                f"<code>{new_path}</code><br><br>"
                "It isn't visible in the list right now — adjust "
                "the Type/State filters, or switch the project "
                "root in the main window to see it.")
            return

        # ``select_skill`` blocks signals (it's designed for the
        # restore-on-cancel-discard flow §7.51), so it won't trigger
        # ``on_skill_selected``. Call the slot explicitly so the
        # middle and right panels populate with the new skill.
        self.skill_list.select_skill(new_skill)
        self.on_skill_selected(new_skill)

        # Intentionally NOT auto-opening SKILL.md in the Editor tab
        # post-create — the previous iteration of this flow did,
        # which violated the right-panel's state-driven tab rule
        # (§7.26): Editor/Preview tabs should be visible iff a
        # *file* is selected in the middle file tree. Auto-open
        # bypassed the file tree and pushed Editor content into the
        # right panel without anything in the file tree showing as
        # active — confusing visual state. Now the post-create view
        # is identical to a normal "user just clicked a skill in
        # the left panel" view: file tree populated, Description tab
        # rendering the new SKILL.md template. To start editing, the
        # user clicks SKILL.md in the file tree — one extra click,
        # no invariant break.

        # Modal acknowledgment of the successful creation. Replaces
        # an earlier status-bar flash — a 5-second transient is too
        # easy to miss, and a creation event is a meaningful enough
        # action that an explicit "click to dismiss" gesture is
        # appropriate. ``QMessageBox.information`` (not ``warning``)
        # because nothing went wrong: the icon signals success.
        QMessageBox.information(
            self,
            "Skill created",
            f"The {new_skill.type.value.lower()} skill "
            f"<b>{new_skill.name}</b> was successfully created.")

    def _find_skill_by_md_path(self, md_path: Path) -> Skill | None:
        """Locate a Skill in the live ``SkillListPanel`` by its
        ``skill_md_path``. Returns None if the path isn't present
        in the current list (e.g. filter-hidden, or the rescan
        missed it). Cheap linear scan — skill counts are in the
        low hundreds at worst."""
        target = md_path.resolve()
        for skill in self.skill_list.all_skills():
            if (skill.skill_md_path is not None
                    and skill.skill_md_path.resolve() == target):
                return skill
        return None

    # ----------------------------------------------------------- menu bar
    def _build_menus(self) -> None:
        """Wire the menu bar: File / Window / Help.

        Action shortcuts coexist with toolbar shortcuts via
        ``ApplicationShortcut`` context where useful. ``QAction`` with
        a shortcut auto-renders the chord in the menu's right gutter."""
        menubar = self.menuBar()

        # ---- File ----
        file_menu = menubar.addMenu("&File")
        act_new_skill = QAction("&New Skill…", self)
        act_new_skill.setShortcut(QKeySequence("Ctrl+N"))
        act_new_skill.triggered.connect(self.open_new_skill_dialog)
        file_menu.addAction(act_new_skill)
        file_menu.addSeparator()
        act_refresh = QAction("&Refresh Skills", self)
        act_refresh.setShortcut(QKeySequence.Refresh)  # F5
        act_refresh.triggered.connect(self.refresh)
        file_menu.addAction(act_refresh)
        file_menu.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # ---- Window ----
        # Populated lazily from ``_test_dialogs`` via
        # ``_rebuild_window_menu``. Kept on ``self`` because we mutate
        # it on every dialog open / close. Constructed BEFORE Help so
        # the menu bar order is File / Window / Resource / Help.
        self._window_menu = menubar.addMenu("&Window")
        self._window_menu.aboutToShow.connect(self._rebuild_window_menu)
        self._rebuild_window_menu()

        # ---- Resource ----
        # Populated once at startup from the packaged ``ai_tools.md``
        # table (see ``claude_skills_manager/ai_tools.py``). The data
        # is static for the lifetime of the process, so unlike the
        # Window menu we don't rebuild on ``aboutToShow``.
        self._resource_menu = menubar.addMenu("&Resource")
        self._populate_resource_menu()

        # ---- Help ----
        help_menu = menubar.addMenu("&Help")
        # Ctrl+Shift+T (not Ctrl+T) — §7.69 reclaimed Ctrl+T for the
        # frequent "Test Skill…" action on the selected skill. The
        # connection check is rare-use enough that the extra Shift
        # modifier is a low cost; the chord is still discoverable in
        # the menu's right gutter.
        act_check = QAction("&Test Claude Connection", self)
        act_check.setShortcut(QKeySequence("Ctrl+Shift+T"))
        act_check.triggered.connect(self.open_check_claude_dialog)
        help_menu.addAction(act_check)
        # Ctrl+L = Log. Free chord (no QLineEdit / QTextEdit /
        # QTreeWidget default claims it), mnemonic, fits the
        # Help-menu accelerator family alongside Ctrl+Shift+T.
        # The chord is set directly on the menu QAction (rather
        # than via _install_shortcuts) because this entry has no
        # right-click / context-menu parallel — the menu QAction
        # itself is the persistent one, so there's no second
        # QAction to collide with and no need for the §7.69
        # ``\t<chord>`` label trick. Qt auto-renders the chord in
        # the menu's right gutter from setShortcut.
        act_open_logs = QAction("&Open Log Folder", self)
        act_open_logs.setShortcut(QKeySequence("Ctrl+L"))
        act_open_logs.triggered.connect(self._open_log_folder)
        help_menu.addAction(act_open_logs)
        # Ctrl+, — the near-universal "Preferences/Settings"
        # chord across VS Code, macOS apps, JetBrains, browsers.
        # Cross-app muscle memory beats any more "local" pick
        # like Ctrl+S (which collides with editor save semantics
        # users expect) or Ctrl+P (Print/Quick-Open elsewhere).
        # Same direct-setShortcut pattern as Open Log Folder
        # above — no context-menu parallel, no ambiguity risk.
        act_settings = QAction("&Settings…", self)
        act_settings.setShortcut(QKeySequence("Ctrl+,"))
        act_settings.triggered.connect(self._open_settings_dialog)
        help_menu.addAction(act_settings)
        help_menu.addSeparator()
        act_about = QAction("&About", self)
        act_about.triggered.connect(self._open_about_dialog)
        help_menu.addAction(act_about)

    def _rebuild_window_menu(self) -> None:
        """Re-populate the Window menu from the live ``_test_dialogs``
        map. Cheap (the map is small); called from every site that
        adds or removes an entry, plus ``aboutToShow`` as a backstop
        in case any path forgot to invalidate.

        Each row is a custom ``_WindowMenuRow`` widget wrapped in a
        ``QWidgetAction`` so the row can host two click zones: the
        title area (raises the window) and a close X button (closes
        it). The placeholder for the empty state stays a plain
        QAction — no per-row affordances to surface there."""
        if not hasattr(self, "_window_menu"):
            return
        self._window_menu.clear()
        # Sort by window title so the menu order is stable and
        # readable (alphabetical by skill name).
        entries = sorted(
            self._test_dialogs.items(),
            key=lambda kv: kv[1].windowTitle().lower(),
        )
        if not entries:
            placeholder = QAction(
                "(no open Skill Test windows)", self._window_menu)
            placeholder.setEnabled(False)
            self._window_menu.addAction(placeholder)
            return
        # Bulk "Close All" affordance at the top of the menu — hidden
        # when there's nothing open. Plain QAction (not a custom
        # _WindowMenuRow) so the visual hierarchy makes the bulk op
        # distinct from the per-dialog rows below. Italicized label
        # via QAction.setFont is overkill — the separator already
        # signals the boundary.
        act_close_all = QAction(
            f"Close All ({len(entries)})", self._window_menu)
        act_close_all.setToolTip(
            "Close every open Skill Test window. Each dialog's own "
            "cancel-on-close logic runs (in-flight `claude` runs are "
            "killed); no test data is persisted across closes.")
        act_close_all.triggered.connect(self._close_all_test_dialogs)
        self._window_menu.addAction(act_close_all)
        self._window_menu.addSeparator()
        for _, dialog in entries:
            row = _WindowMenuRow(dialog.windowTitle())
            # Default-argument captures the dialog by value at
            # connection time, sidestepping Python's late-binding
            # closure semantics in the for-loop.
            row.raise_requested.connect(
                lambda d=dialog: self._raise_dialog_from_menu(d))
            row.close_requested.connect(
                lambda d=dialog: self._close_dialog_from_menu(d))

            action = QWidgetAction(self._window_menu)
            action.setDefaultWidget(row)
            self._window_menu.addAction(action)

    def _raise_dialog_from_menu(self, dialog) -> None:
        """Raise the dialog AND dismiss the menu. QWidgetAction
        widgets don't auto-close their parent menu on click, so we
        close it explicitly — otherwise the menu lingers above the
        window the user just asked to see."""
        self._window_menu.close()
        self._raise_dialog(dialog)

    def _close_all_test_dialogs(self) -> None:
        """Close every open Test Skill dialog. Triggered from the
        Window menu's "Close All" entry.

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
        self._window_menu.close()
        for dialog in list(self._test_dialogs.values()):
            dialog.close()

    def _close_dialog_from_menu(self, dialog) -> None:
        """Close the dialog AND dismiss the menu. ``dialog.close()``
        triggers ``WA_DeleteOnClose`` → emits ``closed`` →
        ``_on_test_dialog_closed`` → ``_rebuild_window_menu``, so the
        row will be gone the next time the user opens Window. No
        manual map maintenance needed here."""
        self._window_menu.close()
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

    def _populate_resource_menu(self) -> None:
        """Build the Resource menu structure. Called exactly once,
        from ``_build_ui`` at startup.

        Layout (top to bottom):

        * A ``QLineEdit`` search field embedded via ``QWidgetAction``
          — case-insensitive substring filter on tool name. Cleared
          and refocused on every menu open (see
          ``_on_resource_menu_about_to_show``) so search is
          transient and predictable.
        * Separator.
        * Pinned "Edit Resource…".
        * "Sort by" submenu carrying three exclusive radio entries
          (default file order / A→Z / Z→A).
        * Separator.
        * One QAction per parsed AITool — sorted by the persisted
          user preference (QSettings key ``resource_sort``) and
          filtered by the current search text.

        Why the one-shot full build (vs. rebuilding from scratch
        every time): clicking a sort radio while the Sort submenu is
        open must not tear the submenu down — that would collapse
        the menu chain mid-click and defeat ``_StayOpenMenu``. Sort
        changes, search keystrokes, and Edit Resource… saves all
        route through ``_render_tool_actions`` instead, which
        touches only the tool QActions below the *last* separator
        and leaves the search box, Edit Resource…, the Sort
        submenu, and its action group intact."""
        self._resource_menu.clear()

        # Search field at the top — QLineEdit hosted by a
        # QWidgetAction so the line edit captures its own keystrokes
        # without dismissing the menu. Wrapped in a small container
        # widget purely for padding; bare QLineEdits inside QMenus
        # look cramped against the menu's edge. ClearButton lets the
        # user wipe the filter via mouse without clicking through the
        # whole field.
        search_container = QWidget()
        search_layout = QHBoxLayout(search_container)
        search_layout.setContentsMargins(6, 4, 6, 4)
        self._resource_search_edit = QLineEdit()
        self._resource_search_edit.setPlaceholderText("Search resources…")
        self._resource_search_edit.setClearButtonEnabled(True)
        self._resource_search_edit.setMinimumWidth(240)
        # Leading search-glyph icon. Reuses the codebase's existing
        # search_icon() (the toolbar's filter affordance) for visual
        # consistency across the app.
        self._resource_search_edit.addAction(
            search_icon(), QLineEdit.LeadingPosition)
        # textChanged fires per-keystroke — _render_tool_actions is
        # cheap (small lists, just QAction creation), so no debounce
        # needed. The slot ignores the emitted str argument and
        # re-reads from the line edit, so the same render path works
        # from every caller (sort change, edit-resource save, keystroke).
        self._resource_search_edit.textChanged.connect(
            lambda _text="": self._render_tool_actions())
        search_layout.addWidget(self._resource_search_edit)

        search_action = QWidgetAction(self._resource_menu)
        search_action.setDefaultWidget(search_container)
        self._resource_menu.addAction(search_action)
        self._resource_menu.addSeparator()

        edit_action = QAction("&Edit Resource…", self)
        edit_action.triggered.connect(self._open_edit_resource_dialog)
        self._resource_menu.addAction(edit_action)

        # Sort submenu — ``_StayOpenMenu`` keeps the parent Resource
        # menu open when a radio is clicked, so the user can compare
        # sort modes without re-summoning Resource each time. The
        # QActionGroup is parented to the submenu (Qt cleans both up
        # together when the main window is destroyed).
        sort_menu = _StayOpenMenu("&Sort by", self._resource_menu)
        self._resource_menu.addMenu(sort_menu)
        sort_group = QActionGroup(sort_menu)
        sort_group.setExclusive(True)
        initial_mode = self._resource_sort_mode()
        for mode, label in (
            (_RESOURCE_SORT_DEFAULT, "&Default (file order)"),
            (_RESOURCE_SORT_ASC, "A → Z"),
            (_RESOURCE_SORT_DESC, "Z → A"),
        ):
            act = QAction(label, sort_group)
            act.setCheckable(True)
            if mode == initial_mode:
                act.setChecked(True)
            # ``m=mode`` freezes the value at iteration time — same
            # late-binding guard as the per-tool lambda below.
            act.triggered.connect(
                lambda _checked=False, m=mode: self._set_resource_sort(m))
            sort_menu.addAction(act)

        self._resource_menu.addSeparator()

        # Wire menu-open behaviour: clear the search field and hand
        # focus to it. Done here (after the line edit exists) and
        # connected exactly once because _populate_resource_menu is
        # itself one-shot per main-window lifetime.
        self._resource_menu.aboutToShow.connect(
            self._on_resource_menu_about_to_show)

        # Tool entries live below the last separator and are
        # re-rendered in place whenever the sort changes, the search
        # text changes, or Edit Resource… commits a save.
        self._render_tool_actions()

    def _on_resource_menu_about_to_show(self) -> None:
        """Reset the search field and focus it whenever the Resource
        menu opens.

        Clearing the field triggers ``textChanged`` →
        ``_render_tool_actions``, so the tool list also refreshes
        against the current ``ai_tools.md`` state — covers the
        otherwise-missed case where the file is mutated by Edit
        Resource… while the menu is closed.

        ``setActiveAction(None)`` un-arms the first menu item so a
        stray Enter on the line edit can't accidentally trigger Edit
        Resource…. Focus has to be set via ``QTimer.singleShot(0,
        …)`` because ``aboutToShow`` fires *before* the menu is
        actually shown — calling ``setFocus()`` directly on a
        still-hidden widget gets re-routed away once the menu
        becomes visible."""
        self._resource_search_edit.clear()
        self._resource_menu.setActiveAction(None)
        QTimer.singleShot(0, self._resource_search_edit.setFocus)

    def _render_tool_actions(self) -> None:
        """Replace the tool QActions at the bottom of the Resource
        menu with a freshly-loaded, filtered, and sorted list.
        Touches *only* the actions below the *last* separator —
        the search box, Edit Resource…, the Sort submenu, and the
        intermediate separators are preserved so a click in any of
        them (especially the still-open Sort submenu, see
        ``_StayOpenMenu``) doesn't yank the menu chain out from
        under the user.

        Last-separator (not first) because the search field
        introduced a second separator above Edit Resource…; the
        tool list begins after whichever separator comes last,
        regardless of how many sit above it. Robust against future
        menu restructuring without further changes here.

        Three terminal states:

        * ``(no resources available)`` — ``ai_tools.md`` parsed to an
          empty list (fresh install, malformed file, etc.).
        * ``(no matches)`` — file has tools but the current search
          filter hides them all.
        * One QAction per surviving tool — the normal path.

        Removed QActions are ``deleteLater``-ed because
        ``removeAction`` only unlinks them from the menu; they
        would otherwise accumulate as dangling children of
        ``self._resource_menu`` across every keystroke and sort
        change."""
        tools = load_ai_tools()
        sort_mode = self._resource_sort_mode()
        query = self._resource_search_text()

        actions = self._resource_menu.actions()
        last_sep_idx: int | None = None
        for i, a in enumerate(actions):
            if a.isSeparator():
                last_sep_idx = i
        if last_sep_idx is not None:
            for stale in actions[last_sep_idx + 1:]:
                self._resource_menu.removeAction(stale)
                stale.deleteLater()

        if not tools:
            placeholder = QAction("(no resources available)", self._resource_menu)
            placeholder.setEnabled(False)
            self._resource_menu.addAction(placeholder)
            return

        filtered = self._filtered_tools(tools, query)
        if not filtered:
            placeholder = QAction("(no matches)", self._resource_menu)
            placeholder.setEnabled(False)
            self._resource_menu.addAction(placeholder)
            return

        for tool in self._sorted_tools(filtered, sort_mode):
            act = QAction(tool.name, self._resource_menu)
            # ``t=tool`` freezes the binding at iteration time —
            # without it, Python's late closure binding would have
            # every entry open the dialog for the *last* tool.
            act.triggered.connect(
                lambda _checked=False, t=tool: self._open_ai_tool_dialog(t))
            self._resource_menu.addAction(act)

    def _resource_search_text(self) -> str:
        """Current search filter text, stripped of surrounding
        whitespace. Returns ``""`` when the line edit hasn't been
        constructed yet — covers the narrow window between
        ``MainWindow.__init__`` and ``_build_ui``'s
        ``_populate_resource_menu`` call, during which the attribute
        does not yet exist."""
        edit = getattr(self, "_resource_search_edit", None)
        return edit.text().strip() if edit is not None else ""

    @staticmethod
    def _filtered_tools(tools: list[AITool], query: str) -> list[AITool]:
        """Case-insensitive substring match on tool name only.
        Empty / whitespace-only query returns ``tools`` unchanged so
        the no-filter path is a no-op.

        Name-only by design (per UX choice): users typically know
        what the tool is called, and matching summary text would
        return surprising hits like "Anthropic" matching every row
        that mentions Anthropic in its description."""
        if not query:
            return tools
        q = query.lower()
        return [t for t in tools if q in t.name.lower()]

    def _resource_sort_mode(self) -> str:
        """Return the persisted Resource-menu sort mode.

        Unknown / missing values collapse to the default file-order
        mode — covers fresh installs (no key yet) and the unlikely
        case of a hand-edited registry value outside the valid set.
        """
        mode = QSettings(_ORG, _APP).value(
            "resource_sort", _RESOURCE_SORT_DEFAULT)
        if isinstance(mode, str) and mode in _RESOURCE_SORT_VALID:
            return mode
        return _RESOURCE_SORT_DEFAULT

    def _set_resource_sort(self, mode: str) -> None:
        """Persist ``mode`` and re-render *only* the tool list so the
        new order takes effect immediately while the Sort submenu —
        which the user is actively clicking in — stays open. The
        ``_StayOpenMenu`` mouseReleaseEvent override has already
        prevented the menu chain from closing; here we just refresh
        the data the user will see when they navigate back to the
        parent Resource menu."""
        QSettings(_ORG, _APP).setValue("resource_sort", mode)
        self._render_tool_actions()

    @staticmethod
    def _sorted_tools(tools: list[AITool], mode: str) -> list[AITool]:
        """Apply ``mode`` to ``tools`` and return the ordered list.

        ``default`` returns ``tools`` unchanged (preserving the row
        order users curate via Edit Resource…); the alphabetical
        modes sort case-insensitively on the tool name."""
        if mode == _RESOURCE_SORT_ASC:
            return sorted(tools, key=lambda t: t.name.lower())
        if mode == _RESOURCE_SORT_DESC:
            return sorted(tools, key=lambda t: t.name.lower(), reverse=True)
        return tools

    def _open_ai_tool_dialog(self, tool: AITool) -> None:
        AIToolDialog(tool, self).exec()

    def _open_edit_resource_dialog(self) -> None:
        """Open the modal CRUD editor. Always refresh the tool list
        when the dialog closes — Save no longer accepts the dialog
        (it persists in-place so the user can keep editing), so the
        old ``if exec() == Accepted`` guard would never fire.
        Reading the file is cheap and idempotent, so unconditional
        refresh is the simplest correct behaviour.

        ``_render_tool_actions`` (rather than ``_populate_resource_menu``)
        is the right entry point: the dialog can change tool rows
        but not the user's sort preference or the Sort submenu, so a
        full rebuild would needlessly tear down ``_StayOpenMenu`` and
        its action group."""
        EditResourceDialog(self).exec()
        self._render_tool_actions()

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
                self, "Open Log Folder",
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
        UI rows in place — no full rescan needed for a one-skill change.

        Plugin skills land in ``~/.claude/settings.local.json`` via the
        same ``write_override`` call (see §7.63); the only difference
        from Global/Project is the scope, computed by
        ``_scope_dir_for``."""
        scope_dir = self._scope_dir_for(skill)
        if scope_dir is None:
            QMessageBox.warning(
                self, "Cannot toggle",
                f"Couldn't determine the settings scope for "
                f"{skill.name!r}. Try restarting the app.")
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
        # The override layer always reflects the user's click. The
        # composed `state` only mirrors the override when the plugin
        # layer isn't already overriding visibility — for plugin-off
        # rows the composed state stays "plugin-off" because the
        # parent plugin still gates the skill in Claude Code.
        skill.override_state = new_state
        if skill.state != STATE_PLUGIN_OFF:
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

    def _on_skill_drop(
        self, source: Skill, target_type: SkillType,
    ) -> None:
        """Handle a drag-drop of a Project skill onto the Global header
        (§7.66). Confirm Copy / Move / Cancel, then dispatch to
        :mod:`skill_relocate`.

        UI gating already restricts this to Project → Global at the
        widget layer (drag flag set only on Project rows, drop flag
        set only on Global header). The guards here defend against a
        future caller emitting the signal directly with disallowed
        combinations, mirroring the
        :meth:`_on_delete_skill_requested` defensive Plugin check
        pattern."""
        if source.type == SkillType.PLUGIN:
            QMessageBox.warning(
                self, "Cannot relocate",
                "Plugin skills can't be relocated from this GUI — they "
                "are upstream artifacts managed via /plugin in Claude "
                "Code.")
            return
        if target_type == SkillType.PLUGIN:
            QMessageBox.warning(
                self, "Cannot relocate",
                "Plugin scope is upstream-only. Plugins are installed "
                "via /plugin in Claude Code.")
            return

        # Resolve destination first. ``resolve_destination`` does the
        # same scope-dir computation as the mutating helpers but
        # without touching disk, so we can pre-check collisions and
        # render From / To in the confirmation dialog with the real
        # destination path.
        try:
            destination = resolve_destination(
                source, target_type,
                project_root=self._project_root,
                home=None,
            )
        except ValueError as exc:
            QMessageBox.critical(
                self, "Cannot relocate",
                f"Couldn't compute the destination: {exc}")
            return

        # Pre-confirmation collision check (§7.66 Polish step). If a
        # skill of the same folder name already lives in Global, the
        # mutation would fail at the copytree step anyway — surfacing
        # the failure as a clean upfront error is more useful than
        # presenting Copy / Move / Cancel buttons that all lead to
        # the same error.
        if destination.exists():
            QMessageBox.critical(
                self, "Skill already exists in Global",
                f"A skill called <b>{source.name}</b> already exists "
                f"in the Global scope:<br><br>"
                f"<code>{destination}</code><br><br>"
                "Rename or remove one of them first, then try again.")
            return

        # Close any open Test Skill dialog for the source skill — its
        # ``_skill.path`` would go stale the moment we move/copy. The
        # ``WA_DeleteOnClose`` flag tears down state cleanly; the user
        # can reopen post-Refresh and the dialog will point at the
        # new location.
        existing_dialog = self._test_dialogs.get(source.path)
        if existing_dialog is not None:
            existing_dialog.close()

        # Confirmation dialog. ``QMessageBox`` with three custom-named
        # buttons matches the user's spec verbatim. Default = Cancel
        # so Esc/Enter without reading lands on the safe action.
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Question)
        confirm.setWindowTitle("Move or copy skill to Global?")
        confirm.setText(
            f"Relocate skill <b>{source.name}</b> to Global?")
        confirm.setInformativeText(
            f"<span style='color:#555;'>From:</span> "
            f"<code>{source.path}</code><br>"
            f"<span style='color:#555;'>To:</span> "
            f"<code>{destination}</code><br><br>"
            "<b>Copy</b> leaves the Project skill in place.<br>"
            "<b>Move</b> sends the Project skill to the Recycle Bin "
            "after copying.")
        copy_btn = confirm.addButton("Copy", QMessageBox.AcceptRole)
        move_btn = confirm.addButton("Move", QMessageBox.AcceptRole)
        cancel_btn = confirm.addButton("Cancel", QMessageBox.RejectRole)
        confirm.setDefaultButton(cancel_btn)
        confirm.exec()
        clicked = confirm.clickedButton()
        if clicked is cancel_btn or clicked is None:
            return

        # Dispatch to skill_relocate. Both copy_skill and move_skill
        # share the same RelocationCollision / OSError failure surface
        # so the handlers funnel through one try/except. move_skill
        # additionally raises PartialMoveError when copy succeeds but
        # the source removal fails — surfaced as a "demoted to Copy"
        # message rather than a hard failure.
        try:
            if clicked is copy_btn:
                new_path = copy_skill(
                    source, target_type,
                    project_root=self._project_root,
                )
                verb = "Copied"
            else:
                new_path = move_skill(
                    source, target_type,
                    project_root=self._project_root,
                )
                verb = "Moved"
        except RelocationCollision as exc:
            # Race against an external creator between our existence
            # check above and the actual copytree. Rare in practice
            # but the cleanest surface is a fresh error dialog
            # repeating the collision message.
            QMessageBox.critical(
                self, "Skill already exists in Global", str(exc))
            return
        except ValueError as exc:
            QMessageBox.critical(
                self, "Cannot relocate", str(exc))
            return
        except PartialMoveError as exc:
            # Copy succeeded; source recycle failed. The new copy is
            # fully populated and the persisted session id has been
            # migrated. Tell the user the Move was demoted to a Copy
            # and they'll need to remove the source manually.
            QMessageBox.warning(
                self, "Move demoted to Copy",
                f"Skill <b>{source.name}</b> was copied to Global, "
                f"but the original Project copy at "
                f"<code>{exc.source_path}</code> couldn't be moved "
                f"to the Recycle Bin:<br><br>"
                f"<code>{exc.cause}</code><br><br>"
                "Please remove the original manually.")
            # Still rescan — the new copy is real and the user should
            # see it. Select-by-md-path will land on the new Global
            # entry post-refresh.
            self.refresh()
            self._select_skill_at_path(exc.new_path)
            return
        except OSError as exc:
            QMessageBox.critical(
                self, "Couldn't relocate skill",
                f"Failed to relocate <b>{source.name}</b>:<br><br>"
                f"<code>{exc}</code>")
            return

        self.statusBar().showMessage(
            f"{verb} {source.name} to Global", 4000)
        self.refresh()
        # Re-select the relocated skill in its new Global location so
        # the user has visual confirmation. For Copy, two rows now
        # share the name (Project + Global); selecting the new Global
        # entry matches the user's just-completed gesture.
        self._select_skill_at_path(new_path)

    def _select_skill_at_path(self, path: Path) -> None:
        """Find the post-refresh Skill whose folder path equals
        ``path``, visually select it, AND populate the middle/right
        panels with its contents. No-op if no match (skill was
        filtered out by the current type/state checkboxes or the
        search box — selection clears silently).

        ``select_skill`` alone only updates the highlight — it
        intentionally suppresses ``skill_selected`` so it can serve
        as the "restore highlight after rejected switch" primitive.
        For the drag-drop completion path (§7.66) we WANT the
        panels to populate, so we follow up with an explicit
        :meth:`on_skill_selected` call — same effect as if the
        user had clicked the row directly.

        Mirrors the existing :meth:`_find_skill_by_md_path` shape
        but keyed on the folder path rather than the SKILL.md
        path, which is what :mod:`skill_relocate` returns from its
        mutators."""
        try:
            resolved = path.resolve()
        except OSError:
            return
        for skill in self.skill_list.all_skills():
            try:
                if skill.path.resolve() == resolved:
                    if self.skill_list.select_skill(skill):
                        # Populate file_tree / skill_info /
                        # editor_panel as a user click would.
                        self.on_skill_selected(skill)
                    return
            except OSError:
                continue

    def _scope_dir_for(self, skill: Skill) -> Path | None:
        """Return the .claude directory whose settings.local.json controls
        this skill's overrides.

        Plugin skills target ``~/.claude`` — plugin skill overrides are
        user-global (the layer they sit on top of, ``enabledPlugins``,
        is also user-global). Global/Project skills target the
        ``.claude/`` folder above their own ``skills/`` directory
        (``skill.path.parents[1]``). This must mirror the read scope in
        ``SkillScanner._populate_states`` — see §7.15 and §7.63."""
        if skill.type == SkillType.PLUGIN:
            return Path.home() / ".claude"
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
        # Type / State checkbox states are intentionally NOT persisted.
        # Each launch starts with all five filters checked (the
        # ``setChecked(True)`` defaults in ``_build_toolbar``) so the
        # user sees the full skill inventory on open and applies
        # filters fresh per session. Stale ``show_*`` keys from
        # earlier builds are actively removed so they don't linger in
        # the registry forever.
        s = QSettings(_ORG, _APP)
        s.setValue("project_root", str(self._project_root) if self._project_root else "")
        for stale in ("show_global", "show_project", "show_plugin",
                      "show_enabled", "show_disabled"):
            s.remove(stale)
        s.setValue("geometry",      self.saveGeometry())
        s.setValue("state",         self.saveState())

    def _restore_settings(self) -> None:
        # Filter checkboxes deliberately skipped — see ``_save_settings``.
        s = QSettings(_ORG, _APP)
        root = s.value("project_root") or ""
        if root and Path(root).exists():
            self._project_root = Path(root)
            self._update_root_label()
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
