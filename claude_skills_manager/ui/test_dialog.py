"""Modeless dialog for testing a skill against an ad-hoc prompt (§7.34).

Opens from the main window via the toolbar "Test Skill…" button
(Ctrl+T) or the skill-list right-click menu. Modeless — stays open
while the user interacts with the main window; one dialog instance per
skill (``MainWindow`` enforces, raising an existing window if the user
re-opens for the same skill).

The dialog has two read-only tabs plus one interactive test runner:

* **Description** — the skill's ``SKILL.md`` body (frontmatter
  stripped) rendered as markdown. Same content as the main window's
  Skill Description tab, but standalone so the user can refer to the
  skill copy while testing.
* **Raw SKILL.md** — the full file as-is (frontmatter + body), with
  the same hand-rolled markdown syntax highlighter the editor uses.
* **Test** — prompt input + response viewer + Run/Cancel/Clear
  controls. The runner shells out to ``claude -p <prompt>`` via
  ``QProcess`` so the test executes against the *user's* installed
  Claude Code with all of its skill state (``enabledPlugins`` +
  ``skillOverrides``) honored. See §7.34 / Approach A for the
  rationale on using the CLI rather than the Anthropic SDK direct.

The dialog is destroyed on close (``WA_DeleteOnClose``) and emits
``closed`` with the skill's absolute path so ``MainWindow`` can drop
it from the per-skill instance map."""
from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QFont, QKeySequence, QShortcut, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QStyle, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)


_logger = logging.getLogger("test_dialog")


def _log(*args) -> None:
    """Emit a debug line through the configured file logger.

    Previously printed to stderr; now goes to the per-launch log file
    via the standard logging pipeline. Same call sites, same intent
    (breadcrumbs for diagnosing test-run hangs), but the trail
    persists past the lifetime of any visible terminal."""
    try:
        _logger.info("%s", " ".join(str(a) for a in args))
    except Exception:
        # Logging itself failing would be diagnostic, but not worth
        # crashing the dialog over.
        pass

from .. import app_settings
from ..claude_trust import (
    claude_config_path, is_path_trusted, mark_path_trusted,
)
from ..models import Skill, SkillType
from ..skill_introspect import (
    CLAUDE_EXECUTABLE, build_claude_command, claude_env_overrides,
    claude_path_diagnostic, extract_summary,
    find_claude_executable, parse_claude_json_envelope,
    read_skill_md_text,
)
from ..skill_md import (
    estimated_token_count, parse_skill_md_text, strip_frontmatter,
)
from ..skill_settings import (
    STATE_NAME_ONLY, STATE_OFF, STATE_ON, STATE_PLUGIN_OFF,
    STATE_USER_INVOCABLE_ONLY,
)
from ._icons import test_icon
from ._styles import BUTTON_STYLE
from .code_editor import CodeEditor
from .syntax import highlighter_for_extension


# Tab-bar stylesheet for clear selected-state contrast. Same shape as
# the editor panel's _TAB_STYLE so the look is consistent across the
# app. Applied to BOTH the outer tab bar (Description / Raw SKILL.md /
# Claude) and the inner Response / Raw Output bar.
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


# Hard timeout for a single test run, in milliseconds. Read from
# user settings (Help → Settings… → Test timeout) at each run so a
# change applies immediately to the next click of Run, without
# re-opening the dialog. Default 3 minutes — generous for typical
# ``claude -p`` round-trips while keeping the dialog from looking
# permanently hung if the CLI gets stuck on auth or rate-limit
# backoff. Past this point the run is killed and labeled as
# timed-out so the user has a concrete verdict.
def _test_run_timeout_ms() -> int:
    return app_settings.get_test_timeout_ms()


class TestSkillDialog(QDialog):
    """Modeless test dialog for one skill.

    Lifecycle:

    1. ``MainWindow.open_test_dialog(skill)`` constructs one and calls
       ``show()`` (not ``exec()``).
    2. User runs zero or more tests inside it.
    3. User closes via the X button, the Close button, or Esc. The
       dialog kills any running ``QProcess``, emits ``closed``, then
       is destroyed by Qt (via ``WA_DeleteOnClose``).

    Multi-instance is fine — ``MainWindow`` keeps a ``{skill.path:
    dialog}`` map so a second open request for the same skill raises
    the existing window rather than creating a duplicate."""

    # Emitted just before the dialog is destroyed. Carries the skill's
    # absolute path — the same key MainWindow indexes its
    # active-dialogs map on. Kept as ``object`` to sidestep the
    # ``Path`` registration boilerplate for Qt signals.
    closed = Signal(object)

    # Worker → GUI thread plumbing (§7.43, re-introduced after §7.42
    # fixed the cursor.End bug that was clobbering every prior async
    # attempt). The runner is a ``subprocess.Popen`` running in a
    # ``threading.Thread``; the worker emits one of these signals
    # when finished. Qt's signal system auto-routes cross-thread
    # emissions via ``Qt::QueuedConnection``, so the slot bodies
    # always run on the GUI thread where widget updates are legal.
    #
    # ``_worker_result``: process completed (normally, with non-zero
    #     exit, or because we killed it from Cancel/Timeout).
    #     Payload: (exit_code, stdout_text, stderr_text).
    # ``_worker_failed``: ``Popen`` or ``communicate()`` raised
    #     (FileNotFoundError, OSError, etc.).
    #     Payload: human-readable error string.
    _worker_result = Signal(int, str, str)
    _worker_failed = Signal(str)

    # Stable tab indices — match the build order in `_build_ui`.
    # Description / Raw SKILL.md / Claude.
    _DESCRIPTION_TAB = 0
    _RAW_TAB = 1
    _TEST_TAB = 2

    def __init__(self, skill: Skill, parent: QWidget | None = None) -> None:
        # Pass parent so the dialog inherits the main window's
        # QApplication context (and shows centered over it on first
        # paint), but immediately undo the modality default — QDialog
        # constructs ApplicationModal by default, which would block
        # the main window.
        super().__init__(parent)
        self.setWindowModality(Qt.NonModal)
        # Render as a top-level window with min/max/close buttons —
        # the user can minimize the test window independently. Without
        # this flag QDialog ships with the dialog-only frame (close
        # button only on Windows), which feels wrong for a long-lived
        # secondary window.
        self.setWindowFlags(Qt.Window)
        # Auto-cleanup on close. Combined with the ``closed`` signal,
        # MainWindow doesn't have to hold a strong reference; closing
        # the window fully reclaims its memory.
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._skill = skill
        # Subprocess + worker-thread state (§7.43). The worker
        # thread owns ``_subproc`` from the moment ``Popen``
        # constructs it; the GUI thread reads ``_subproc`` under
        # ``_worker_lock`` to send it ``kill()`` from Cancel /
        # Timeout. Lock is what makes the race between
        # cancel-during-spawn safe — without it the GUI could see
        # ``_subproc = None`` even though the worker is about to
        # assign a live process.
        self._subproc: subprocess.Popen | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        # Wall-clock anchor for the run duration display.
        # ``time.monotonic`` (not ``time.time``) so the duration is
        # immune to NTP drift / DST jumps during the run.
        self._run_started_at: float | None = None
        # Set when the user clicks Cancel; the worker-result handler
        # uses it to label the verdict as "Cancelled" instead of
        # "Exit N".
        self._was_cancelled = False
        # Bytes count for the verdict marker. Populated from the
        # final stdout + stderr lengths in ``_on_worker_result``.
        self._received_bytes: int = 0
        # 500ms tick timer that updates the elapsed-time display
        # *independently* of stdout arriving. Without it, ``claude -p``
        # in its default text-output mode buffers the entire response
        # and emits it at the end — meaning ``readyReadStandardOutput``
        # doesn't fire for 10+ seconds and the status label stays
        # frozen at "Starting…" with no evidence the process is alive.
        # See §7.35 for the diagnosis chain.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(500)
        self._tick_timer.timeout.connect(self._on_tick)
        # Hard timeout — kills a run that's exceeded the 3-minute
        # cap (§7.39). Single-shot so it doesn't need manual reset
        # after firing; started inside ``_on_run`` once the process
        # has actually launched.
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)
        # Set true when the timeout fires, so ``_on_finished`` can
        # label the result correctly. Parallel to ``_was_cancelled``.
        self._timed_out = False
        # Multi-turn conversation state (§7.46). ``_session_id`` is the
        # id Claude Code returned in the previous successful
        # JSON-envelope run; the next Run with the "Continue
        # conversation" checkbox ticked passes it back as
        # ``--resume <id>`` so claude restores the prior context. None
        # means "no captured session yet" — the next run starts fresh.
        # Survives toggling the checkbox (intentional — flipping the
        # box off then on resumes from where you left off). Cleared
        # only by the Clear button and by closing the dialog.
        self._session_id: str | None = None
        # Snapshot of "this run is in continue mode" set at run start
        # and read in ``_on_worker_result``. Stored on self rather
        # than passed through signals because Qt's Signal payload
        # surface is already busy with (exit_code, stdout, stderr) and
        # widening it ripples through every emit site.
        self._last_run_was_continue = False
        # Working directory for the ``claude`` subprocess (§7.48).
        # Defaults to the app's launch directory (``os.getcwd()`` at
        # dialog construction), which exactly preserves the pre-§7.48
        # behavior — the runner inherits the parent's cwd via the
        # explicit ``cwd=`` arg instead of leaving the arg off. Users
        # can override via the Working Directory row's Browse button.
        # Per-dialog state: each Test Skill window has its own cwd;
        # closing and reopening resets to the launch dir.
        self._cwd: Path = Path.cwd()
        # Trust Directory: additional read-access root for this Test
        # Skill window, surfaced as a UI field below cwd and emitted
        # as ``--add-dir <path>`` at Run time (§7.57). Pre-populated
        # with the selected skill's folder so the common "study this
        # skill's SKILL.md" prompt works without manual setup —
        # Global / Plugin skills live outside cwd, so Read would
        # otherwise fail. ``None`` means "field cleared by user" →
        # no --add-dir on the next Run, Read scoped to cwd alone.
        # Skipped at Run when --dangerously-skip-permissions is on
        # (the flag bypasses Read gating entirely). Per-dialog state;
        # closing and reopening re-defaults to the new selection's dir.
        try:
            self._trust_dir: Path | None = (
                self._skill.path.parent.resolve())
        except OSError:
            # Defensive: a skill on a now-unreachable drive
            # shouldn't prevent the dialog from opening. Start
            # empty; the user can Browse later.
            self._trust_dir = None
        # Snapshot of the markdown *source* passed to
        # ``_set_response_markdown`` at the last run. The Response tab
        # is rendered through ``QTextBrowser.setMarkdown`` which loses
        # the original syntax (Qt parses to a rich-text model, and
        # ``toMarkdown`` round-trips with subtle reformatting —
        # fenced-code fences, list bullets, custom HTML embeds). For
        # the Save As… → .md path we save THIS string verbatim, not
        # the QTextDocument's re-serialized form, so what the user
        # saw on stdout is exactly what lands on disk.
        self._last_response_markdown: str = ""

        # SKILL.md is read once at open. The dialog is purely read-only
        # with respect to the file (the editor panel in the main
        # window handles editing); a stale read is acceptable until
        # the user closes & reopens.
        self._raw_skill_md = read_skill_md_text(skill)
        self._metadata, _ = parse_skill_md_text(self._raw_skill_md)
        # Body with frontmatter stripped — used by Description rendering.
        self._body = strip_frontmatter(self._raw_skill_md)

        title_context = _context_label(skill)
        title = f"Test Skill — {skill.name}"
        if title_context:
            title += f"  ({title_context})"
        self.setWindowTitle(title)
        self.setWindowIcon(test_icon())
        self.resize(960, 760)
        self.setMinimumSize(720, 560)

        # Wire worker → GUI signals before building UI so the
        # connections are in place by the time any worker could
        # possibly emit.
        self._worker_result.connect(self._on_worker_result)
        self._worker_failed.connect(self._on_worker_failed)

        self._build_ui()
        self._wire_shortcuts()

    # ------------------------------------------------------------- UI build
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(_TAB_STYLE)
        # Build order matches the user-requested tab order:
        # Description, Raw SKILL.md, Claude.
        self._build_description_tab()
        self._build_raw_tab()
        self._build_test_tab()
        # Default-select the Claude tab — the dialog's primary purpose
        # is to run a prompt, so landing the user there one step sooner.
        # The other tabs remain available for the reading/reference flow.
        self.tabs.setCurrentIndex(self._TEST_TAB)
        layout.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(BUTTON_STYLE)
        close_btn.setShortcut(QKeySequence(Qt.Key_Escape))
        close_btn.clicked.connect(self.close)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

    def _build_header(self) -> QLabel:
        """Build the always-visible header strip with name / context /
        type / state / path / mtime / tokens.

        Rich text rather than a grid layout because:
        * Selectable as a single text block (user can copy any field).
        * Wraps gracefully when the dialog narrows.
        * A grid would force fixed-width label columns that look
          out of proportion when most rows are short (Type, State)."""
        skill = self._skill
        context = _context_label(skill)
        state_word = _state_to_label(skill.state)
        type_word = skill.type.value
        token_count = estimated_token_count(self._raw_skill_md) if self._raw_skill_md else 0
        mod_str = _format_mtime(skill.skill_md_path)
        summary = extract_summary(self._metadata, self._body)

        # Title: name (large bold) + optional faded context suffix.
        title_html = f"<span style='font-size:14pt; font-weight:bold; color:#1a1a1a;'>{html.escape(skill.name)}</span>"
        if context:
            title_html += (f"<span style='font-size:12pt; color:#888; "
                           f"font-weight:normal;'> · {html.escape(context)}</span>")

        # Metadata line: bold type chip + state + token count + mtime.
        meta_html = (f"<b>{html.escape(type_word)}</b>"
                     f" · {html.escape(state_word)}"
                     f" · ≈{token_count:,} tokens"
                     f" · Modified {html.escape(mod_str)}")

        # Path on its own line in monospace — long paths get the
        # whole horizontal budget and visual cue (monospace = "this
        # is a path / a thing you can paste") without competing with
        # the title.
        path_html = (f"<span style='font-family: Consolas, monospace; "
                     f"font-size:9pt;'>{html.escape(str(skill.path))}</span>")

        parts = [f"<div>{title_html}</div>"]
        if summary:
            parts.append(f"<div style='color:#444; margin-top:4px; "
                         f"font-style:italic;'>{html.escape(summary)}</div>")
        parts.append(f"<div style='color:#666; margin-top:6px; "
                     f"font-size:9pt;'>{meta_html}</div>")
        parts.append(f"<div style='color:#888; margin-top:2px;'>{path_html}</div>")

        header = QLabel("".join(parts))
        header.setTextFormat(Qt.RichText)
        header.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.setWordWrap(True)
        return header

    def _build_description_tab(self) -> None:
        """Markdown render of SKILL.md body — same renderer / strip
        rules as the main window's Skill Description tab, but reads a
        one-time snapshot rather than re-rendering from a live editor
        buffer. Stale-while-the-dialog-is-open is acceptable here."""
        view = QTextBrowser()
        self.description_view = view  # exposed for _scroll_all_to_top
        view.setOpenExternalLinks(True)

        skill = self._skill
        # Header block mirroring the main window's description style:
        # name + blockquoted description + horizontal rule + body.
        name = self._metadata.get("name") if isinstance(self._metadata.get("name"), str) else skill.name
        description = (self._metadata.get("description") if isinstance(self._metadata.get("description"), str)
                       else skill.description)
        parts: list[str] = [f"# {name}"]
        if description:
            parts.append(f"> {description}")
        if self._body.strip():
            parts.append("---")
            parts.append(self._body.strip())
        elif not description:
            parts.append("*(SKILL.md missing or empty)*")
        view.setMarkdown("\n\n".join(parts))

        self.tabs.addTab(view, "Description")

    def _build_raw_tab(self) -> None:
        """Full SKILL.md (frontmatter + body) in a read-only editor with
        the same markdown highlighter the editor panel uses for ``.md``
        files. Read-only via ``setReadOnly`` rather than swapping in a
        ``QTextBrowser`` so the syntax highlighter still runs (the
        highlighter expects a ``QTextDocument``, which both editors
        provide but ``QTextBrowser`` styles separately)."""
        editor = CodeEditor()
        self.raw_editor = editor  # exposed for _scroll_all_to_top
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(10)
        editor.setFont(font)
        editor.setPlainText(self._raw_skill_md or "(SKILL.md not found)")
        editor.setReadOnly(True)
        # Keep a reference so the highlighter isn't GC'd alongside its
        # transient builder — ``highlighter_for_extension`` returns the
        # QSyntaxHighlighter, which holds a reference to the document
        # but isn't held by it; without this attribute it would dangle.
        self._raw_highlighter = highlighter_for_extension(".md", editor.document())
        self.tabs.addTab(editor, "Raw SKILL.md")

    def _build_test_tab(self) -> None:
        """Interactive test runner. Vertical splitter so the user can
        rebalance space between prompt input (usually a few lines) and
        the response area (often many).

        Layout:
        * Top: "Prompt:" label + multi-line editor + control row
          (Run / Cancel / Clear / status / busy bar).
        * Bottom: "Response:" label + read-only plain-text view.

        Plain-text response (not markdown-rendered) is intentional —
        during streaming, reflowing a markdown render on every chunk
        is visually janky and obscures the streaming-as-it-arrives
        feel. Plain text matches what the user would see in their own
        terminal."""
        splitter = QSplitter(Qt.Vertical)

        # ---- Top half: prompt + controls ----
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)

        # Working Directory row (§7.48). Sits above the prompt
        # header so it reads as a per-window setting that governs
        # every Run, not as something tied to one specific prompt.
        # Layout: label + read-only path display + Browse button.
        # Read-only display (instead of an editable QLineEdit) so the
        # user can't paste a malformed path that wouldn't survive
        # QFileDialog's "must exist" guarantee — and so the only
        # mutation path is the Browse button, which keeps the value
        # in lock-step with an actual directory on disk.
        cwd_row = QHBoxLayout()
        self._cwd_label = QLabel("Working Directory:")
        cwd_row.addWidget(self._cwd_label)
        self.cwd_display = QLineEdit(str(self._cwd))
        self.cwd_display.setReadOnly(True)
        self.cwd_display.setToolTip(
            "Directory `claude` will be invoked from for every Run "
            "in this window. Affects which project memory file "
            "(~/.claude/projects/<slug>/memory/MEMORY.md) and which "
            "project-local settings (.claude/settings.local.json) the "
            "subprocess loads. Click Browse to change.")
        # Monospace for paths — same convention used by the dialog's
        # header strip and the Raw Output pane.
        cwd_font = QFont("Consolas")
        cwd_font.setStyleHint(QFont.Monospace)
        cwd_font.setPointSize(9)
        self.cwd_display.setFont(cwd_font)
        cwd_row.addWidget(self.cwd_display, 1)
        self.cwd_browse_btn = QPushButton("Browse…")
        self.cwd_browse_btn.setStyleSheet(BUTTON_STYLE)
        self.cwd_browse_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.cwd_browse_btn.setToolTip(
            "Pick a different working directory for this Test Skill "
            "window. Changing cwd does NOT clear the active "
            "conversation session — click Clear if you want a fresh "
            "session from the new directory.")
        self.cwd_browse_btn.clicked.connect(self._on_browse_cwd)
        cwd_row.addWidget(self.cwd_browse_btn)
        # Match heights: a QPushButton styled with BUTTON_STYLE has
        # padding that makes it visibly taller than a default
        # QLineEdit (especially with the 9pt monospace font we set
        # above for the path display). Size the line edit to the
        # button's natural height so they read as one row.
        # ``sizeHint()`` is authoritative here — the button already
        # has its stylesheet and icon applied, so its preferred
        # height accounts for both. Apply via setFixedHeight (not
        # setMinimumHeight) because we want the two to remain
        # locked even if the layout tries to redistribute space.
        self.cwd_display.setFixedHeight(
            self.cwd_browse_btn.sizeHint().height())
        top_layout.addLayout(cwd_row)

        # Trust Directory row (§7.57). Sits right below Working
        # Directory because it answers a related question (what can
        # ``claude`` Read this run?) with a related shape (path +
        # Browse). Pre-populated with the selected skill's folder so
        # prompts like ``study this skill 'C:\...\SKILL.md' ...``
        # work out of the box for Global / Plugin skills (which live
        # outside any user-chosen cwd). Clearable so the user can
        # explicitly scope Read back to cwd alone.
        #
        # Naming caveat: "Trust" here is the Claude CLI's ``--add-dir``
        # grant, NOT the ~/.claude.json ``hasTrustDialogAccepted`` flag.
        # Same word, different mechanism. The two are distinguishable
        # by scope: the JSON flag persists across runs; this field is
        # per-window. Comments / docstrings use "additional read path"
        # when precision matters.
        trust_row = QHBoxLayout()
        self._trust_label = QLabel("Trust Directory:")
        trust_row.addWidget(self._trust_label)
        self.trust_dir_display = QLineEdit(
            str(self._trust_dir) if self._trust_dir else "")
        self.trust_dir_display.setReadOnly(True)
        self.trust_dir_display.setPlaceholderText(
            "(none — Read scoped to working directory)")
        self.trust_dir_display.setToolTip(
            "Additional directory `claude` will be granted Read on for "
            "every Run in this window (passed as --add-dir). "
            "Pre-populated with the selected skill's folder so prompts "
            "that reference SKILL.md by absolute path can read it. "
            "Click the × inside the field to limit Read to the working "
            "directory; click Browse to pick a different directory.")
        self.trust_dir_display.setFont(cwd_font)
        self.trust_dir_display.textChanged.connect(
            self._on_trust_dir_text_changed)
        # Inline clear "×" — custom action, NOT Qt's built-in
        # ``setClearButtonEnabled``. Qt's built-in action is internally
        # gated by ``!isReadOnly()`` (see
        # ``QLineEditPrivate::updateClearButton``), so on a read-only
        # field the icon renders but the click is a no-op. Adding our
        # own action at ``TrailingPosition`` bypasses that gate: we
        # own the action's enabled/visible state, and the slot calls
        # ``QLineEdit.clear()`` which is not read-only-gated. Same
        # icon (``SP_LineEditClearButton``) as the built-in for visual
        # parity.
        self._trust_clear_action = QAction(
            self.style().standardIcon(QStyle.SP_LineEditClearButton),
            "Clear Trust Directory",
            self.trust_dir_display,
        )
        self._trust_clear_action.setToolTip(
            "Clear Trust Directory. Next Run will be invoked without "
            "--add-dir, scoping Read to the working directory only.")
        self._trust_clear_action.triggered.connect(
            self._on_clear_trust_dir_action)
        self.trust_dir_display.addAction(
            self._trust_clear_action,
            QLineEdit.ActionPosition.TrailingPosition,
        )
        # Hide the action until there's text to clear — mirrors Qt's
        # native clear button's "only when non-empty" behaviour. The
        # ``textChanged`` slot keeps this in sync afterwards.
        self._trust_clear_action.setVisible(
            bool(self.trust_dir_display.text()))
        trust_row.addWidget(self.trust_dir_display, 1)
        self.trust_dir_browse_btn = QPushButton("Browse…")
        self.trust_dir_browse_btn.setStyleSheet(BUTTON_STYLE)
        self.trust_dir_browse_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.trust_dir_browse_btn.setToolTip(
            "Pick a directory to grant `claude` Read access on for "
            "runs in this window. Translates to a --add-dir flag at "
            "Run time. Skipped when 'Skip permission prompts' is "
            "checked — that flag bypasses the Read gate entirely, "
            "making --add-dir redundant.")
        self.trust_dir_browse_btn.clicked.connect(
            self._on_browse_trust_dir)
        trust_row.addWidget(self.trust_dir_browse_btn)
        # Same height-lock trick as the cwd row — keep the read-only
        # QLineEdit visually aligned with the styled buttons.
        self.trust_dir_display.setFixedHeight(
            self.trust_dir_browse_btn.sizeHint().height())
        top_layout.addLayout(trust_row)

        # Column-align the Working Directory and Trust Directory rows:
        # pin both labels to the same (max) sizeHint width so the
        # QLineEdits in each row start at the same x and — since the
        # Browse buttons are identical width — end at the same x too.
        # ``sizeHint().width()`` already accounts for the label's font
        # and any margins; ``setFixedWidth`` (not setMinimumWidth)
        # because we want the lock to survive layout redistribution.
        # A QFormLayout would do this automatically but would require
        # restructuring both rows — overkill for two labels.
        _row_label_width = max(
            self._cwd_label.sizeHint().width(),
            self._trust_label.sizeHint().width(),
        )
        self._cwd_label.setFixedWidth(_row_label_width)
        self._trust_label.setFixedWidth(_row_label_width)

        # Header row: "Prompt:" label + session indicator (left of
        # the checkboxes when active) + Continue conversation +
        # Prefix Skill Name. The checkboxes sit on the same line as
        # the label so the relationship to the prompt below is
        # unambiguous (vs. floating them elsewhere).
        prompt_header = QHBoxLayout()
        prompt_header.addWidget(QLabel("Prompt:"))
        prompt_header.addStretch(1)
        # Session indicator — empty until a continue-mode run captures
        # a session id. Faded so it doesn't compete visually with the
        # active controls.
        self.session_label = QLabel("")
        self.session_label.setStyleSheet("color:#888; font-size:9pt;")
        self.session_label.setToolTip(
            "Active Claude conversation id. Shown when a Continue-"
            "conversation run has captured a session — the next Run "
            "with the checkbox ticked will resume from here.")
        prompt_header.addWidget(self.session_label)
        # Continue-conversation toggle (§7.46). Checked by default —
        # multi-turn context is the strongly-expected behavior for a
        # window where users naturally ask follow-up prompts ("now
        # expand on point 2", "what about X"); the cost of a stray
        # JSON envelope parse on a one-shot run is far smaller than
        # the cost of every follow-up forgetting the conversation.
        # When checked, the next Run includes --resume <session-id>
        # (if we have one) and switches to --output-format json so we
        # can capture the session id from the envelope for subsequent
        # turns.
        self.continue_checkbox = QCheckBox("Continue conversation")
        self.continue_checkbox.setToolTip(
            "When checked, each Run continues the previous run's "
            "session so Claude remembers context across turns. "
            "Uncheck (or click Clear) to start a fresh conversation. "
            "The session id appears on the left once captured.")
        self.continue_checkbox.setChecked(True)
        prompt_header.addWidget(self.continue_checkbox)
        self.prefix_checkbox = QCheckBox("Prefix Skill Name")
        self.prefix_checkbox.setToolTip(
            "When checked, prepends /<skill-name> to the prompt — Claude "
            "Code's gesture for user-invoking a specific skill. Toggling "
            "adds or removes the prefix immediately.")
        self.prefix_checkbox.setChecked(True)
        self.prefix_checkbox.toggled.connect(self._on_prefix_toggled)
        prompt_header.addWidget(self.prefix_checkbox)
        # Skip-permission-prompts toggle (§7.50). Default OFF — the
        # safe default matches `claude --print`'s own safe default
        # (deny tool calls without explicit per-prompt approval).
        # When ON, the next Run appends `--dangerously-skip-permissions`
        # so the subprocess auto-approves every Write / Bash / network
        # tool the skill wants to use. This is essential for testing
        # skills that *do* things (scaffolders, file-emitters) and
        # dangerous for testing untrusted prompts. The "dangerous"
        # framing is preserved in both the label and the tooltip so
        # there's no chance the user toggles it without understanding.
        self.skip_perms_checkbox = QCheckBox("Skip permission prompts")
        self.skip_perms_checkbox.setToolTip(
            "When checked, passes --dangerously-skip-permissions to "
            "`claude`. The subprocess will auto-approve EVERY tool "
            "use (file writes, shell commands, network requests) "
            "without prompting. Required for testing skills that "
            "create files or run commands, since `claude --print` "
            "has no interactive way to ask for approval — denied "
            "tools just respond with 'not approved.'\n\n"
            "Leave UNCHECKED unless you trust the prompt AND the cwd "
            "is a scratch directory you don't mind being written to. "
            "Per-window state — closing the dialog resets to "
            "unchecked.")
        self.skip_perms_checkbox.setChecked(False)
        prompt_header.addWidget(self.skip_perms_checkbox)
        top_layout.addLayout(prompt_header)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Type a prompt to send to `claude` (Ctrl+Enter to run)…")
        # Use a slightly larger font than the response — the user is
        # typing here, the response is for reading.
        prompt_font = QFont()
        prompt_font.setPointSize(10)
        self.prompt_edit.setFont(prompt_font)
        # Seed the prompt with the skill-name prefix so the dialog
        # opens ready to run. Matches the checkbox's initial state.
        self.prompt_edit.setPlainText(self._skill_prefix() + " ")
        # Place the cursor AFTER the prefix so typing starts at the
        # right spot without an immediate Right-arrow keystroke.
        cursor = self.prompt_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.prompt_edit.setTextCursor(cursor)
        top_layout.addWidget(self.prompt_edit, 1)

        bar = QHBoxLayout()
        # Standard Qt icons for Cancel / Clear via QStyle.standardIcon —
        # gives the buttons a recognizable shape without bundling assets.
        style = self.style()
        self.run_btn = QPushButton("Run")
        self.run_btn.setStyleSheet(BUTTON_STYLE)
        self.run_btn.setIcon(test_icon())
        self.run_btn.setDefault(True)
        self.run_btn.setToolTip(
            "Send the prompt above to `claude` (Ctrl+Enter)")
        self.run_btn.clicked.connect(self._on_run)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet(BUTTON_STYLE)
        self.cancel_btn.setIcon(style.standardIcon(QStyle.SP_DialogCancelButton))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setToolTip(
            "Stop the running `claude` process. Anything received so "
            "far stays visible.")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setStyleSheet(BUTTON_STYLE)
        self.clear_btn.setIcon(style.standardIcon(QStyle.SP_DialogResetButton))
        self.clear_btn.setToolTip(
            "Clear the prompt and the response panes. Doesn't affect a "
            "run in progress.")
        self.clear_btn.clicked.connect(self._on_clear)
        bar.addWidget(self.run_btn)
        bar.addWidget(self.cancel_btn)
        bar.addWidget(self.clear_btn)
        bar.addSpacing(16)

        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet("color:#666;")
        bar.addWidget(self.status_label)
        bar.addSpacing(8)

        # Indeterminate progress bar shown only while running, same
        # design choice as the main window's busy bar (§7.33): we don't
        # know the duration in advance, so a marquee is the honest
        # signal.
        self.run_busy = QProgressBar()
        self.run_busy.setRange(0, 0)
        self.run_busy.setTextVisible(False)
        self.run_busy.setFixedSize(120, 12)
        self.run_busy.hide()
        bar.addWidget(self.run_busy)
        bar.addStretch(1)
        top_layout.addLayout(bar)

        splitter.addWidget(top)

        # ---- Bottom half: nested Response / Raw Output tabs (§7.44) ----
        # Two views over the same run: the **Response** tab renders
        # ``claude``'s stdout as markdown (the clean answer), the
        # **Raw Output** tab keeps the chronological monospace dump
        # (diagnostic preface + raw stdout + stderr + verdict
        # markers). Different audiences for different moments —
        # "I want the answer" vs. "I want to debug what claude did."
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(4)

        self._response_tabs = QTabWidget()
        self._response_tabs.setDocumentMode(True)
        self._response_tabs.setStyleSheet(_TAB_STYLE)

        # ---- Corner widget: "Save As…" (§7.51) ----
        # One button serves both inner tabs; its target depends on
        # which tab is current. Placed at TopRightCorner so it sits
        # in the empty area to the right of the "Response / Raw
        # Output" tab labels, mirroring how VS Code / Claude Desktop
        # park context-sensitive actions in tab strips.
        #
        # Style follows BUTTON_STYLE (dark text on a filled light
        # surface) instead of a flat blue-on-transparent — the
        # earlier flat variant rendered with low contrast on
        # Windows ClearType, where saturated blue text on a near-
        # white background bleaches into "looks white" at small
        # font sizes. A filled, bordered surface plus dark text
        # gives reliable contrast across themes. Padding matches
        # the tab cell's vertical metric (``6px`` top/bottom) so
        # the button cap aligns with the tab caption baseline; an
        # explicit setFixedHeight at the end of _build_test_tab
        # snaps any residual gap to zero.
        self._save_as_btn = QPushButton("Save As…")
        self._save_as_btn.setStyleSheet("""
            QPushButton {
                background: #f5f5f5;
                border: 1px solid #b0b0b0;
                border-radius: 3px;
                padding: 6px 14px;
                color: #1a1a1a;
            }
            QPushButton:hover:enabled {
                background: #e8eef9;
                border-color: #2d6cdf;
                color: #1a1a1a;
            }
            QPushButton:pressed:enabled {
                background: #d6dff0;
                border-color: #1f4e9e;
            }
            QPushButton:disabled {
                background: #fafafa;
                color: #aaaaaa;
                border-color: #d8d8d8;
            }
        """)
        self._save_as_btn.setCursor(Qt.PointingHandCursor)
        self._save_as_btn.clicked.connect(self._on_save_as_clicked)
        self._response_tabs.setCornerWidget(
            self._save_as_btn, Qt.TopRightCorner)
        # Re-evaluate enabled state and tooltip whenever the user
        # switches between Response and Raw Output — the button
        # describes whichever tab is currently visible.
        self._response_tabs.currentChanged.connect(
            self._update_save_btn_state)

        # ---- Tab 1: Response (rendered markdown) ----
        # ``QTextBrowser.setMarkdown`` is the same renderer the Skill
        # Description tab uses. Qt's CommonMark parser handles
        # headers, lists, fenced code, blockquotes, and the
        # ``★ Insight ─────────`` boxes the user's prompts often
        # contain (those are just text + a horizontal rule).
        self.response_view = QTextBrowser()
        self.response_view.setOpenExternalLinks(True)
        self.response_view.setPlaceholderText(
            "(Rendered response will appear here when `claude` finishes)")
        self._response_tabs.addTab(self.response_view, "Response")

        # ---- Tab 2: Raw Output (chronological monospace) ----
        # The previous single-pane behavior, moved here verbatim.
        # Diagnostic preface goes here at the start of each run; raw
        # stdout, any stderr, and the verdict marker (`[done in Xs]`
        # / `[cancelled]` / `[timed out]` / `[error]`) follow.
        self.raw_view = QPlainTextEdit()
        self.raw_view.setReadOnly(True)
        raw_font = QFont("Consolas")
        raw_font.setStyleHint(QFont.Monospace)
        raw_font.setPointSize(10)
        self.raw_view.setFont(raw_font)
        self.raw_view.setPlaceholderText(
            "(Diagnostic + raw output will appear here when you click Run)")
        self._response_tabs.addTab(self.raw_view, "Raw Output")

        bottom_layout.addWidget(self._response_tabs, 1)
        splitter.addWidget(bottom)

        # Response gets more vertical real estate by default; users
        # can drag the splitter to rebalance if they want a larger
        # prompt area.
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([220, 560])

        container = QWidget()
        cl = QVBoxLayout(container)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.addWidget(splitter)
        self.tabs.addTab(container, "Claude")

        # Snap the corner Save As… button's height to the tab bar's
        # so the cap-line of the button aligns flush with the tab
        # caption row instead of floating short. Same idiom used
        # for the Working Directory row's Browse-vs-display
        # alignment — pick the neighbor whose height is canonical
        # and pin the smaller widget to it. Computed AFTER both
        # tabs are added so ``tabBar().sizeHint()`` reflects the
        # real laid-out height (it grows once the first tab is
        # inserted; querying earlier returns a smaller minimum).
        bar_height = self._response_tabs.tabBar().sizeHint().height()
        if bar_height > 0:
            self._save_as_btn.setFixedHeight(bar_height)

        # Initial Save As… state: both panes are empty at construct,
        # so the button starts disabled with a "run something first"
        # tooltip. Subsequent renders / appends / clears refresh it.
        self._update_save_btn_state()

    # ---- Working-directory control -----------------------------------------
    def _on_browse_cwd(self) -> None:
        """Open a native folder picker rooted at the current cwd; on
        accept, replace ``self._cwd`` and the displayed path. Uses
        ``QFileDialog.getExistingDirectory`` so the returned path is
        guaranteed to be an existing directory at selection time —
        the user can't pick a file or a non-existent path. (If the
        directory disappears between selection and Run, Popen raises
        and the existing _worker_failed path surfaces a clean error
        message.)

        Intentionally does NOT clear ``_session_id``. Changing the
        working directory can shift the project-memory context the
        next ``claude`` invocation loads, but §7.47's principle holds:
        forget is a user gesture, not an automatic consequence. The
        Clear button is the explicit way to reset; this slot just
        updates the cwd."""
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Pick working directory for this Test Skill window",
            str(self._cwd),
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if not chosen:
            return  # user cancelled — leave cwd unchanged
        new_cwd = Path(chosen)
        if new_cwd == self._cwd:
            return  # picked the same one — no need to log a change
        _log(f"working directory changed: {self._cwd} -> {new_cwd}")
        self._cwd = new_cwd
        self.cwd_display.setText(str(new_cwd))
        # Proactive hint — prompt for trust now so the user knows
        # the policy before they type a prompt. Decline is non-fatal;
        # the Run-click boundary will ask again, and the existing
        # "Skip permission prompts" checkbox is still an escape hatch.
        # Per the action-boundary rule (feedback-checkbox-invariant-
        # at-action), the *load-bearing* check is at Run, not here.
        self._ensure_cwd_trusted(new_cwd, ask_user=True)

    def _on_browse_trust_dir(self) -> None:
        """Open a folder picker rooted at the current Trust Directory
        (or the selected skill's directory if the field is empty), set
        ``self._trust_dir`` and the displayed path. Mirrors
        ``_on_browse_cwd`` — same widget pattern, different state —
        except this field is allowed to be empty (user opts out of the
        ``--add-dir`` grant via Clear).

        Does NOT pre-trust the directory in ~/.claude.json. ``--add-dir``
        is a runtime Read grant scoped to the next invocation; the
        ``hasTrustDialogAccepted`` flag is a persistent per-directory
        gate. They're separate by design — the cwd row owns the
        persistent gate; this row owns the per-run grant."""
        start = (
            str(self._trust_dir) if self._trust_dir
            else str(self._skill.path.parent))
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Pick a directory to grant Read access on for this window",
            start,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if not chosen:
            return  # user cancelled — leave field unchanged
        new_dir = Path(chosen)
        if self._trust_dir == new_dir:
            return  # picked the same one — no log noise
        _log(f"trust directory changed: {self._trust_dir} -> {new_dir}")
        self._trust_dir = new_dir
        self.trust_dir_display.setText(str(new_dir))

    def _on_trust_dir_text_changed(self, text: str) -> None:
        """Sync the inline clear action's visibility with text presence,
        and reset ``self._trust_dir`` when the field becomes empty.

        ``textChanged`` fires for every text mutation: programmatic
        ``setText`` from :meth:`_on_browse_trust_dir`, the inline
        clear action's slot calling ``QLineEdit.clear()``, etc. We do
        two cheap things every time: toggle the action icon's
        visibility (so it appears only when there's text to clear),
        and on the empty transition reset ``self._trust_dir`` so the
        next Run omits ``--add-dir``."""
        # Guard against this firing before _build_ui finishes wiring
        # the action (the QLineEdit's initial text triggers no signal,
        # so in practice this is just belt-and-braces).
        if hasattr(self, "_trust_clear_action"):
            self._trust_clear_action.setVisible(bool(text))
        if text:
            return
        if self._trust_dir is None:
            return  # already None — nothing to log or sync
        _log(f"trust directory cleared (was {self._trust_dir})")
        self._trust_dir = None

    def _on_clear_trust_dir_action(self) -> None:
        """Triggered by the inline "×" action. Clears the QLineEdit;
        ``_on_trust_dir_text_changed`` then resets ``self._trust_dir``
        on the resulting empty-text signal, so the two stay in sync.

        ``QLineEdit.clear()`` is NOT read-only-gated (unlike Qt's
        built-in clear-button action), which is why we own this
        action rather than using ``setClearButtonEnabled``."""
        self.trust_dir_display.clear()

    # ---- Trust-this-folder gate --------------------------------------------
    def _ensure_cwd_trusted(self, path: Path, *, ask_user: bool) -> bool:
        """Mirror Claude Desktop's "Trust this folder?" gesture so a
        non-interactive ``claude -p`` invocation in ``path`` doesn't
        hang on the CLI's interactive trust prompt.

        Returns True iff ``path`` is trusted on return:

        * Already trusted → True (no UI, no write).
        * ``~/.claude.json`` doesn't exist → True (CLI not initialized;
          our auto-trust would create a stub that erases CLI state the
          next ``claude`` run expects to find. Pass through and let the
          CLI's own first-run dialog handle initialization.)
        * Not trusted, ``ask_user`` False → False (caller deferred the
          prompt; e.g. silent recheck path).
        * Not trusted, ``ask_user`` True → show the confirmation. On
          accept, write the trust flag and return True. On decline or
          write failure, return False.

        Decoupled from "Skip permission prompts" deliberately. Trust
        and per-tool permissions are two different gates inside the
        CLI: trust is whether the directory is allowed at all; tool
        permissions are which operations are allowed once trusted.
        Caller decides whether to invoke this check based on which
        gate the run is going to cross."""
        state = is_path_trusted(path)
        if state is True:
            return True
        if state is None:
            return True
        if not ask_user:
            return False
        cfg = claude_config_path()
        resp = QMessageBox.question(
            self,
            "Trust this folder?",
            f"The Claude CLI has not yet trusted this directory:\n\n"
            f"    {path}\n\n"
            f"Trusting allows the CLI to read files in this directory "
            f"and its subdirectories when invoked from this Test Skill "
            f"window. This is the same gesture Claude Desktop offers "
            f"when you open a new project folder.\n\n"
            f"The flag is written to {cfg} under the same key Claude "
            f"itself maintains, so future CLI runs (inside or outside "
            f"this app) will skip the trust prompt for this directory.\n\n"
            f"Trust this folder?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            _log(f"trust declined for {path}")
            return False
        try:
            mark_path_trusted(path)
        except Exception as exc:  # noqa: BLE001 — surfaced to user
            _log(f"trust write failed for {path}: {exc!r}")
            QMessageBox.warning(
                self,
                "Could not record trust flag",
                f"Failed to update {claude_config_path()}:\n\n{exc}\n\n"
                f"You can still proceed by enabling "
                f"'Skip permission prompts'.",
            )
            return False
        _log(f"trust granted for {path}")
        return True

    # ---- Prefix-skill-name helpers -----------------------------------------
    def _skill_prefix(self) -> str:
        """The leading token that represents "invoke this skill" to
        Claude Code (its built-in ``/<name>`` user-invocation gesture).
        Centralized so the toggle, the seed, and the dirty-state check
        all share one definition."""
        return f"/{self._skill.name}"

    def _on_prefix_toggled(self, checked: bool) -> None:
        """Insert or remove the leading ``/<skill> `` token in the
        prompt edit when the user flips the checkbox. The text remains
        editable in either state — the checkbox only manipulates the
        prefix itself; everything the user typed after it survives."""
        prefix = self._skill_prefix()
        text = self.prompt_edit.toPlainText()
        # Match either with or without a single trailing space so a
        # user-edited prompt without that space still toggles cleanly.
        starts_with_space = text.startswith(prefix + " ")
        starts_with_bare = text.startswith(prefix) and not starts_with_space
        already_prefixed = starts_with_space or starts_with_bare

        if checked and not already_prefixed:
            new_text = prefix + " " + text
        elif not checked and starts_with_space:
            new_text = text[len(prefix) + 1:]
        elif not checked and starts_with_bare:
            new_text = text[len(prefix):]
        else:
            return  # no-op — already in the desired state

        # Replace the buffer in one shot (preserves undo as a single
        # edit step) and place the cursor just past the prefix so the
        # user can continue typing where they left off.
        cursor = self.prompt_edit.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        cursor.insertText(new_text)
        cursor.endEditBlock()
        if checked:
            new_cursor = self.prompt_edit.textCursor()
            new_cursor.movePosition(
                QTextCursor.MoveOperation.Start)
            new_cursor.movePosition(
                QTextCursor.MoveOperation.Right,
                QTextCursor.MoveMode.MoveAnchor,
                len(prefix) + 1,
            )
            self.prompt_edit.setTextCursor(new_cursor)

    # ---- Scroll-to-top on first paint --------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 — Qt naming
        """Reset the read-only panes to the top on first show. Qt's
        markdown / plaintext setters leave the cursor at the END of
        the inserted content, which scrolls long Description / Raw
        SKILL.md views to the bottom. Doing this in ``showEvent`` is
        the right hook because the viewports only have a meaningful
        scrollbar after first paint."""
        super().showEvent(event)
        if not getattr(self, "_scrolled_to_top", False):
            self._scrolled_to_top = True
            QTimer.singleShot(0, self._scroll_all_to_top)

    def _scroll_all_to_top(self) -> None:
        """Reset the vertical scrollbar to 0 on every read-only pane.
        Works for ``QTextBrowser`` and ``QPlainTextEdit`` uniformly
        because both expose ``verticalScrollBar()``."""
        widgets = (
            getattr(self, "description_view", None),
            getattr(self, "raw_editor", None),
            getattr(self, "response_view", None),
            getattr(self, "raw_view", None),
        )
        for w in widgets:
            if w is None:
                continue
            sb = w.verticalScrollBar()
            if sb is not None:
                sb.setValue(0)

    def _wire_shortcuts(self) -> None:
        """Ctrl+Enter / Ctrl+Return inside the prompt area runs the
        test — same gesture as most chat UIs, so it transfers without
        the user having to learn anything new. Wired via QShortcut
        scoped to the dialog (not the prompt editor) so it works
        whether or not the editor has keyboard focus."""
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(keys), self)
            sc.activated.connect(self._on_run)

    # --------------------------------------------------------------- runtime
    def _on_run(self) -> None:
        """Async test runner (§7.43).

        Spawns a Python ``threading.Thread`` that runs
        ``subprocess.Popen`` + ``communicate()`` against ``claude``.
        GUI thread stays responsive — the tick timer keeps elapsed
        time updating, Cancel works, and the user can switch tabs /
        read the SKILL.md while waiting.

        Uses the **playground-matching invocation shape**: bare
        ``"claude"``, ``--print``, no ``stdin`` override, no ``cwd``
        override (§7.41). Now that the §7.42 ``cursor.End`` bug is
        fixed, this shape works reliably in a background thread —
        the previous threaded attempts (§7.40) failed because of
        the AttributeError clobbering every output update, not
        because of any actual threading or QProcess problem.

        Wrapped in ``try/except`` so any synchronous setup failure
        lands in the response pane as a visible traceback (§7.37
        silent-swallow defense)."""
        try:
            if self._is_running():
                # Already running — Run button is also disabled in
                # this state, but the keyboard shortcut bypasses
                # the button so we guard.
                return
            prompt = self.prompt_edit.toPlainText().strip()
            if not prompt:
                self.status_label.setText("Type a prompt first")
                return

            # Enforce the Prefix Skill Name checkbox at Run time.
            # Construction-time seeding plants ``/<skill> `` in the
            # prompt buffer, but a user who selects-all and retypes
            # silently wipes the prefix while the checkbox stays
            # ticked — so the checkbox claim "my prompt has the
            # prefix" diverged from reality. The toggle handler only
            # fires on explicit clicks, not buffer edits, so the only
            # robust place to reconcile the two is right before the
            # invocation. We also update the editor so the user sees
            # the prompt that's actually being sent.
            if self.prefix_checkbox.isChecked():
                prefix = self._skill_prefix()
                already_prefixed = (
                    prompt == prefix
                    or prompt.startswith(prefix + " ")
                    or prompt.startswith(prefix + "\n")
                )
                if not already_prefixed:
                    prompt = f"{prefix} {prompt}"
                    cursor = self.prompt_edit.textCursor()
                    cursor.beginEditBlock()
                    cursor.select(QTextCursor.SelectionType.Document)
                    cursor.removeSelectedText()
                    cursor.insertText(prompt)
                    cursor.endEditBlock()

            # Pull live values from Settings on each run. A Settings
            # dialog change therefore applies to the very next click of
            # Run, without reopening this dialog.
            model = app_settings.get_model()
            api_key = app_settings.get_api_key()
            # Continue-mode decision (§7.46): checkbox state at click
            # time, snapshotted onto self for the worker-result slot
            # to see. Resume id is only included when continue is on
            # AND we have a session id from a prior turn — first Run
            # with the box checked has no id yet and runs fresh.
            continue_mode = self.continue_checkbox.isChecked()
            resume_id = self._session_id if continue_mode else ""
            self._last_run_was_continue = continue_mode
            # Permission-skip decision (§7.50). Read at click time —
            # toggling mid-run only affects the *next* Run, never
            # the one already in flight.
            skip_perms = self.skip_perms_checkbox.isChecked()
            # Trust gate at the action boundary. When the user is
            # NOT bypassing permissions, the CLI gates first use of
            # an unfamiliar cwd behind an interactive "Trust this
            # folder?" prompt — which a non-interactive QProcess run
            # can't answer, so the run hangs until timeout. Mirror
            # Claude Desktop's gesture: ask the user once, persist
            # the flag in ~/.claude.json, never prompt again for that
            # directory. Skipped entirely when skip_perms is on
            # because --dangerously-skip-permissions already bypasses
            # this gate inside the CLI.
            if not skip_perms and not self._ensure_cwd_trusted(
                    self._cwd, ask_user=True):
                self.status_label.setText(
                    "Trust required for this directory — Run cancelled.")
                _log("run aborted: trust prompt declined")
                return
            # Read-access grant via --add-dir, sourced from the Trust
            # Directory field (§7.57). Pre-populated on dialog open
            # with the selected skill's folder so the common "study
            # this skill's SKILL.md" prompt works without setup, but
            # the user can Clear (field → None, no grant) or Browse
            # (e.g. test skill A while a prompt reads files from skill
            # B). Skipped when skip_perms is on (the flag bypasses
            # Read gating entirely, so --add-dir would be redundant
            # clutter) and when the trust dir is already inside cwd
            # (Read works through cwd trust; --add-dir would no-op).
            extra_read_dirs: list[Path] = []
            if not skip_perms and self._trust_dir is not None:
                trust_dir = self._trust_dir.resolve()
                try:
                    inside_cwd = trust_dir.is_relative_to(
                        self._cwd.resolve())
                except (OSError, ValueError):
                    inside_cwd = False
                if not inside_cwd:
                    extra_read_dirs.append(trust_dir)
            cmd = build_claude_command(
                prompt,
                model=model,
                session_id=resume_id,
                json_output=continue_mode,
                skip_permissions=skip_perms,
                extra_read_dirs=extra_read_dirs,
            )
            env_overrides = claude_env_overrides(api_key)
            timeout_ms = _test_run_timeout_ms()
            timeout_s = timeout_ms / 1000

            _log("=" * 60)
            _log(f"_on_run START")
            _log(f"  prompt: {prompt!r}")
            _log(f"  cmd: {cmd!r}")
            _log(f"  model: {model!r}  (empty = let claude pick)")
            _log(f"  api_key override: {'yes' if api_key else 'no'}")
            _log(f"  continue: {continue_mode}  resume_id: "
                 f"{resume_id or '(none — fresh session)'}")
            _log(f"  skip_permissions: {skip_perms}")
            # Snapshot the cwd choice now — needed in the diagnostic
            # preface below AND for the worker thread further down.
            # Previous code (§7.48 first cut) deferred this assignment
            # until just before the thread spawn, which crashed
            # `_on_run` with UnboundLocalError when the diagnostic
            # preface referenced `run_cwd` first.
            run_cwd = str(self._cwd)
            _log(f"  cwd: {run_cwd}")
            _log(f"  timeout: {timeout_s:.0f}s")
            _log(f"  skill: {self._skill.name} ({self._skill.type.value})")

            self._clear_run_views()
            self._run_started_at = time.monotonic()
            self._was_cancelled = False
            self._timed_out = False
            self._received_bytes = 0
            if continue_mode and resume_id:
                self.status_label.setText(
                    f"Resuming session {resume_id[:8]}…")
            else:
                self.status_label.setText("Starting…")
            self.run_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)
            self.run_busy.show()

            # Diagnostic preface — rendered shell-like for copy/paste
            # debugging. Conditional lines are omitted in the default
            # case so the simple invocation reads cleanly. The api-key
            # override is acknowledged but its value never logged —
            # don't paint secrets into the user's clipboard.
            diag_lines = [f"$ claude --print {prompt!r}"]
            if model:
                diag_lines.append(f"  --model {model}")
            if resume_id:
                diag_lines.append(f"  --resume {resume_id}")
            if continue_mode:
                diag_lines.append("  --output-format json  "
                                  "(continue mode — parsed for session id)")
            if skip_perms:
                # The flag's name carries its own warning; surface it
                # verbatim in the user-visible Raw Output so there's
                # no doubt about what was passed.
                diag_lines.append(
                    "  --dangerously-skip-permissions  "
                    "(auto-approving every tool call this run)")
            for d in extra_read_dirs:
                # Visible so the user understands which path was
                # opened up for Read on this run. Pairs with the
                # build_claude_command emission above; one diag line
                # per --add-dir keeps the shell-copy paste honest.
                diag_lines.append(
                    f"  --add-dir {d}  "
                    "(read access for the selected skill)")
            # cwd line is always rendered (§7.48). Even in the default
            # "cwd == launch dir" case the user benefits from seeing
            # explicitly which directory drives memory + settings
            # lookup, since the answer is non-obvious without it.
            diag_lines.append(f"  (cwd: {run_cwd})")
            if api_key:
                diag_lines.append("  (env: ANTHROPIC_API_KEY override active)")
            diag_lines.append(
                f"  (timeout: {int(timeout_s)}s; running in "
                f"background — UI stays responsive)")
            diag_lines.append("")
            self._append_raw("\n".join(diag_lines))

            # ``run_cwd`` was assigned above (before the log/diag
            # preface block) — it snapshots the cwd selection at click
            # time, so a mid-run Browse change applies to the *next*
            # Run only and doesn't race with this worker.

            # Spawn the worker thread. Daemon so it won't keep the
            # process alive if the user quits mid-run; Qt's signal
            # routing handles the thread → GUI handoff for results.
            self._worker_thread = threading.Thread(
                target=self._worker_main,
                args=(cmd, env_overrides, run_cwd),
                daemon=True,
                name="ClaudeTestSkillWorker",
            )
            self._worker_thread.start()
            _log(f"worker thread started: {self._worker_thread.name}")

            # Tick timer drives the elapsed-time display.
            self._tick_timer.start()
            # Hard timeout backstop — fires on the GUI thread; kills
            # the subprocess via shared lock if it hasn't returned.
            self._timeout_timer.start(timeout_ms)

        except Exception as e:
            tb = traceback.format_exc()
            _log(f"OUTER EXCEPTION in _on_run:\n{tb}")
            self._append_raw(
                f"\n[INTERNAL ERROR in _on_run]\n{tb}\n")
            self.status_label.setText(f"ERROR: {e}")
            self._teardown_process()

    def _worker_main(
        self,
        cmd: list[str],
        env_overrides: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        """Worker-thread entry point (§7.43). Runs in
        ``ClaudeTestSkillWorker`` thread, NOT the GUI thread —
        widget mutations are forbidden from here; communicate back
        only via Qt signals (which auto-marshall to the GUI thread).

        ``env_overrides`` carries key=value pairs to merge on top of
        ``os.environ`` for the child process — currently
        ``ANTHROPIC_API_KEY`` when the user has set one in Settings.
        ``None`` or empty dict means "inherit parent env as-is",
        which keeps the §7.41 playground-matching invocation shape
        intact for the no-override case.

        ``cwd`` (§7.48) is the working directory the dialog's
        Working Directory control resolved to at click time. When
        equal to ``str(Path.cwd())`` it preserves the §7.41 inherit-
        parent's-cwd shape byte-for-byte (Popen treats ``cwd=str``
        identical to inheritance when the value matches the process
        cwd). When the user picked a different directory via Browse,
        this is the override that makes ``claude`` load that
        location's project memory / settings.

        Two emission paths: ``_worker_result`` on completion (even
        if killed from the GUI thread — that path emits with
        whatever stdout/stderr was drained), or ``_worker_failed``
        if ``Popen`` itself or ``communicate()`` raised."""
        _log(f"[worker] _worker_main START")
        _log(f"[worker] thread: {threading.current_thread().name}")
        _log(f"[worker] env_overrides keys: "
             f"{list(env_overrides.keys()) if env_overrides else []}")
        _log(f"[worker] cwd: {cwd!r}")

        # Build the child env up-front so a None override path stays
        # *exactly* identical to the previous no-env behavior (passing
        # None to Popen inherits the parent env; passing a dict that's
        # a copy of os.environ is observably the same, but None keeps
        # the diff minimal in the no-override case).
        child_env: dict[str, str] | None = None
        if env_overrides:
            child_env = {**os.environ, **env_overrides}

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # cwd is the explicit choice from the dialog's
                # Working Directory control (§7.48). Defaults to
                # ``str(Path.cwd())`` which matches the pre-§7.48
                # inherited-parent behavior — so the no-change case
                # remains observably identical to the playground
                # shape proven in §7.41.
                env=child_env,
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            _log(f"[worker] Popen succeeded, pid={proc.pid}")
        except (FileNotFoundError, OSError, PermissionError) as e:
            _log(f"[worker] Popen failed: {type(e).__name__}: {e}")
            self._worker_failed.emit(f"{type(e).__name__}: {e}")
            return
        except Exception:
            tb = traceback.format_exc()
            _log(f"[worker] Popen unexpected:\n{tb}")
            self._worker_failed.emit(
                f"Unexpected exception during Popen:\n{tb}")
            return

        # Hand the process to the GUI thread for potential
        # kill via Cancel / Timeout — the lock guards the assignment
        # against the GUI thread reading mid-write.
        with self._worker_lock:
            self._subproc = proc

        _log("[worker] calling communicate()…")
        try:
            stdout, stderr = proc.communicate()
            _log(f"[worker] communicate returned: exit={proc.returncode}")
            _log(f"  stdout: {len(stdout)} chars")
            _log(f"  stderr: {len(stderr)} chars")
        except Exception:
            tb = traceback.format_exc()
            _log(f"[worker] communicate raised:\n{tb}")
            self._worker_failed.emit(
                f"communicate() raised:\n{tb}")
            return

        _log("[worker] emitting _worker_result")
        self._worker_result.emit(
            proc.returncode, stdout or "", stderr or "")
        _log("[worker] _worker_main END")

    def _on_worker_result(
        self, exit_code: int, stdout: str, stderr: str,
    ) -> None:
        """``_worker_result`` slot — runs in the GUI thread (Qt
        auto-routes from the worker via ``QueuedConnection``).
        Paints output, classifies the verdict, restores button state."""
        _log(f"[gui] _on_worker_result: exit={exit_code}, "
             f"stdout={len(stdout)} chars, stderr={len(stderr)} chars")

        if stdout:
            # Continue-mode (§7.46) runs ``claude --output-format json``,
            # which serializes the entire turn as a single-line JSON
            # envelope. The ``"result"`` string inside contains literal
            # ``\n`` escapes (two characters, the JSON spelling of a
            # newline) — appending that single line to the Raw Output
            # tab gives a wall-of-text with no actual line breaks. Fix:
            # pretty-print the envelope so the keys lay out vertically,
            # then add a "decoded result" block that re-emits the
            # ``"result"`` field with real newlines — so the user sees
            # both views (envelope structure for debugging, decoded
            # text for reading).
            wrote_pretty = False
            if self._last_run_was_continue:
                try:
                    envelope = json.loads(stdout)
                except (ValueError, TypeError):
                    envelope = None
                if isinstance(envelope, dict):
                    pretty = json.dumps(
                        envelope, indent=2, ensure_ascii=False)
                    self._append_raw(pretty)
                    if not pretty.endswith("\n"):
                        self._append_raw("\n")
                    result_text = envelope.get("result", "")
                    if isinstance(result_text, str) and result_text:
                        self._append_raw(
                            "\n---- decoded result "
                            "(newlines rendered) ----\n")
                        self._append_raw(result_text)
                        if not result_text.endswith("\n"):
                            self._append_raw("\n")
                    wrote_pretty = True
            # Fallback path: not JSON mode, or JSON parse failed. Append
            # stdout verbatim — preserves the legacy behaviour for the
            # non-continue runs and gives a usable view of malformed
            # JSON without losing any bytes.
            if not wrote_pretty:
                self._append_raw(stdout)
                if not stdout.endswith("\n"):
                    self._append_raw("\n")
        if stderr.strip():
            self._append_raw(f"\n[stderr]\n{stderr}")
            if not stderr.endswith("\n"):
                self._append_raw("\n")

        duration = 0.0
        if self._run_started_at is not None:
            duration = time.monotonic() - self._run_started_at
        self._received_bytes = len(stdout) + len(stderr)

        if self._timed_out:
            limit = _test_run_timeout_ms() // 1000
            self.status_label.setText(
                f"TIMED OUT after {duration:.1f}s (limit {limit}s)")
            self._append_raw(
                f"\n[timed out after {duration:.1f}s]\n"
                f"\nTry the prompt manually in a terminal:\n"
                f"  claude --print \"<prompt>\"\n"
                f"If the terminal also hangs, `claude` may be waiting "
                f"on tool-permission input; try "
                f"`--dangerously-skip-permissions` for testing.\n")
            # No useful answer in stdout — render a clear notice in
            # the Response tab so the user doesn't see a blank pane.
            self._set_response_markdown(
                f"**Timed out after {duration:.1f}s.**\n\n"
                f"`claude` did not respond within the deadline. "
                f"See the **Raw Output** tab for troubleshooting "
                f"steps.")
        elif self._was_cancelled:
            self.status_label.setText(
                f"Cancelled after {duration:.1f}s")
            self._append_raw(
                f"\n[cancelled by user after {duration:.1f}s]\n")
            self._set_response_markdown(
                f"*Cancelled by user after {duration:.1f}s.*")
        elif exit_code != 0:
            self.status_label.setText(
                f"Exit {exit_code} after {duration:.1f}s")
            self._append_raw(
                f"\n[exited with code {exit_code} "
                f"after {duration:.1f}s]\n")
            # Render whatever stdout we got, since some commands
            # emit useful output even on non-zero exit (e.g., usage
            # errors). The Raw Output tab has the exit-code marker.
            self._set_response_markdown(stdout)
        else:
            out_tokens = estimated_token_count(stdout)
            self._append_raw(
                f"\n[done in {duration:.1f}s · "
                f"{self._received_bytes:,} chars total]\n")
            # The happy path — render the model's response as
            # markdown so headers, lists, code blocks, and other
            # markup display the way claude intends (§7.44).
            #
            # Continue-mode runs (§7.46) emitted JSON instead of
            # plain markdown — extract the `result` field as the
            # rendered response and capture `session_id` so the next
            # Run can pass --resume <id>. Parse failures fall back
            # to the raw stdout so the user never sees a blank
            # Response tab.
            if self._last_run_was_continue:
                response_text, new_session_id, is_error = (
                    parse_claude_json_envelope(stdout))
                if new_session_id:
                    self._session_id = new_session_id
                    self._update_session_label()
                    _log(f"  captured session_id: {new_session_id}")
                else:
                    _log("  no session_id in JSON envelope; "
                         "next continue Run will start fresh")
                if is_error:
                    response_text = (
                        "**Claude reported an error for this turn.**\n\n"
                        + response_text)
                self.status_label.setText(
                    f"Done in {duration:.1f}s · "
                    f"≈{estimated_token_count(response_text):,} tokens out · "
                    f"session {self._session_id[:8] if self._session_id else '?'}…")
                self._set_response_markdown(response_text)
            else:
                self.status_label.setText(
                    f"Done in {duration:.1f}s · "
                    f"≈{out_tokens:,} tokens out")
                self._set_response_markdown(stdout)

        self._teardown_process()
        _log(f"_on_run END")

    def _on_worker_failed(self, error_msg: str) -> None:
        """``_worker_failed`` slot — Popen or communicate() raised.
        Surfaces the error visibly; renders the §7.38 PATH diagnostic
        for not-found-style failures."""
        _log(f"[gui] _on_worker_failed: {error_msg}")
        is_not_found = (
            "FileNotFoundError" in error_msg
            or "WinError 2" in error_msg
            or "No such file" in error_msg
        )
        if is_not_found:
            diag = claude_path_diagnostic()
            self._append_raw(
                f"\n[error] Could not launch `claude`.\n"
                f"  {error_msg}\n"
                f"\n--- Path resolution diagnostic ---\n"
                f"{diag}\n"
                f"--- End diagnostic ---\n")
            self.status_label.setText(
                "Failed to start — see Raw Output tab for PATH diagnostic")
            self._set_response_markdown(
                "**Could not launch `claude`.**\n\n"
                "The CLI was not found on PATH. See the "
                "**Raw Output** tab for the full path diagnostic, "
                "or use the **Check Claude** button on the main "
                "toolbar to verify your install.")
        else:
            self._append_raw(f"\n[error] {error_msg}\n")
            first_line = (
                error_msg.splitlines()[0][:80] if error_msg else "?")
            self.status_label.setText(f"Worker error: {first_line}")
            self._set_response_markdown(
                f"**Worker error.** See **Raw Output** tab for the "
                f"full traceback.\n\n```\n{first_line}\n```")
        self._teardown_process()

    def _is_running(self) -> bool:
        """True iff the worker thread is alive. Single source of
        truth for "is a run in progress" — used by ``_on_run`` to
        reject re-entry, ``_on_tick`` to gate elapsed-time updates,
        and ``_on_timeout`` to skip stale firings."""
        return (self._worker_thread is not None
                and self._worker_thread.is_alive())

    def _kill_subproc(self) -> None:
        """Kill the running subprocess from the GUI thread. Safe to
        call multiple times — the lock protects the handle and the
        ``poll()`` guard skips an already-exited process."""
        with self._worker_lock:
            proc = self._subproc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                _log(f"[gui] killed subprocess pid={proc.pid}")
            except Exception as e:
                _log(f"[gui] kill failed: {type(e).__name__}: {e}")

    def _on_tick(self) -> None:
        """Tick timer slot (§7.43). Updates the elapsed-time +
        countdown display every 500ms while the worker thread is
        running. Independent of subprocess I/O — fires on Qt's event
        loop, which is free because the work is happening on a
        background thread."""
        if self._run_started_at is None or not self._is_running():
            return
        elapsed = time.monotonic() - self._run_started_at
        timeout_s = _test_run_timeout_ms() / 1000
        remaining = max(0.0, timeout_s - elapsed)
        self.status_label.setText(
            f"Running… {elapsed:.1f}s · waiting for response "
            f"(timeout in {remaining:.0f}s)")

    def _append_raw(self, text: str) -> None:
        """Append to the **Raw Output** tab (§7.44). Used for the
        diagnostic preface, raw stdout chunks, stderr lines, and
        verdict markers — the chronological trace of what happened
        during the run.

        Uses the **fully-qualified** ``QTextCursor.MoveOperation.End``
        enum reference, not the legacy ``cursor.End`` instance
        shorthand. PySide6 6.5+ enforces strict enum scoping by
        default and the shorthand raises ``AttributeError`` — that
        was the root cause of every "hang" symptom in §7.34-§7.41
        (§7.42)."""
        cursor = self.raw_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.raw_view.setTextCursor(cursor)
        self.raw_view.ensureCursorVisible()
        # The Raw Output tab now has saveable content (the first
        # append). Refresh the corner Save As… button state — cheap
        # because _append_raw fires only a handful of times per run
        # (preface + stdout + stderr + verdict), not per character.
        self._update_save_btn_state()

    def _set_response_markdown(self, markdown_text: str) -> None:
        """Set the **Response** tab's rendered content from a
        markdown string (§7.44). Empty / whitespace-only text is
        rendered as a faded "no response" placeholder so the tab
        doesn't look blank after a successful-but-empty run.

        Snapshots ``markdown_text`` into ``_last_response_markdown``
        so the Save As… → .md path can write the *source* the user
        saw, not the QTextDocument's re-serialization (§7.51).

        After rendering, snap both response panes back to the top —
        Qt leaves the cursor at the END of inserted content, which
        scrolls long answers off the top of the viewport. The user
        wants to read from the beginning."""
        # Snapshot BEFORE the render call — the placeholder branch
        # below doesn't change the snapshot, which is intentional:
        # an empty-result run leaves _last_response_markdown empty
        # so the Save As… button stays disabled for that tab.
        self._last_response_markdown = markdown_text
        if markdown_text.strip():
            self.response_view.setMarkdown(markdown_text)
        else:
            # Italics + faded color via inline HTML so the
            # placeholder is visibly distinct from a real response.
            self.response_view.setHtml(
                "<p style='color:#888; font-style:italic;'>"
                "(no response — see Raw Output tab for details)"
                "</p>")
        # Defer the scroll-reset to the next event-loop tick — the
        # viewport's scrollbar range only stabilizes after Qt has
        # laid out the new content.
        QTimer.singleShot(0, self._scroll_response_panes_to_top)
        self._update_save_btn_state()

    def _scroll_response_panes_to_top(self) -> None:
        """Reset both Response and Raw Output to the top. Called
        after every run completes so the user sees the start of the
        ``claude`` answer, not the end-of-stream tail."""
        for w in (self.response_view, self.raw_view):
            sb = w.verticalScrollBar()
            if sb is not None:
                sb.setValue(0)

    def _clear_run_views(self) -> None:
        """Clear both Response and Raw Output for a fresh run.
        Called from ``_on_run`` start and from the Clear button."""
        self.raw_view.clear()
        self.response_view.setMarkdown("")
        # Drop the markdown snapshot too — keeping it would let the
        # Save As… button claim there's a response to save when the
        # tab now visibly shows nothing. Stays in sync with the
        # rule: button enabled ⇔ tab has user-visible content.
        self._last_response_markdown = ""
        self._update_save_btn_state()

    # ---- Save As… (§7.51) --------------------------------------------------
    def _update_save_btn_state(self) -> None:
        """Refresh the corner Save As… button to reflect the
        currently-visible inner tab. Disables when that tab is
        empty (nothing to save), and tunes the tooltip so the user
        knows at a glance which file kind would be written.

        Called from four sites: the inner-tab ``currentChanged``
        signal, ``_set_response_markdown`` (Response tab gains
        content), ``_append_raw`` (Raw Output tab gains content),
        and ``_clear_run_views`` (both tabs lose content). One
        small helper avoids three separate flag toggles drifting
        out of sync."""
        idx = self._response_tabs.currentIndex()
        if idx == 0:  # Response
            has_content = bool(self._last_response_markdown.strip())
            ready_tip = "Save the rendered response as a .md file"
            empty_tip = "Run a prompt first — no response to save yet"
        else:  # Raw Output
            has_content = bool(self.raw_view.toPlainText().strip())
            ready_tip = "Save the raw output (diagnostic + stdout) as a .txt file"
            empty_tip = "Run a prompt first — no raw output to save yet"
        self._save_as_btn.setEnabled(has_content)
        self._save_as_btn.setToolTip(ready_tip if has_content else empty_tip)

    def _on_save_as_clicked(self) -> None:
        """Corner button slot — dispatches by the visible inner tab.
        Disabled-state is also enforced in ``_update_save_btn_state``,
        but the dispatch still re-checks emptiness defensively in
        case a future entry point bypasses the helper."""
        idx = self._response_tabs.currentIndex()
        if idx == 0:
            self._save_response_as()
        else:
            self._save_raw_output_as()

    def _save_response_as(self) -> None:
        """Write the last rendered Response — as the markdown source
        the user saw, not the QTextDocument round-trip — to a .md
        file chosen via the native file dialog. Default name slot:
        ``<skill>-response-<YYYYMMDD-HHMMSS>.md`` rooted at the
        per-window working directory, so the user lands in the same
        folder they've configured the test to run against."""
        text = self._last_response_markdown
        if not text.strip():
            return  # button should be disabled; defensive no-op
        default = self._default_save_filename("response", "md")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Response as Markdown",
            default,
            "Markdown files (*.md);;All files (*)",
        )
        if not path:
            return
        self._write_text_file(Path(path), text, kind_label="response")

    def _save_raw_output_as(self) -> None:
        """Write the Raw Output verbatim to a .txt file. Source is
        ``QPlainTextEdit.toPlainText()`` — that *is* the ground
        truth for the Raw tab (no rich-text layer to round-trip
        through, unlike the Response tab)."""
        text = self.raw_view.toPlainText()
        if not text.strip():
            return  # button should be disabled; defensive no-op
        default = self._default_save_filename("raw-output", "txt")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Raw Output as Text",
            default,
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        self._write_text_file(Path(path), text, kind_label="raw output")

    def _default_save_filename(self, kind: str, ext: str) -> str:
        """Build a default save path: ``<cwd>/<skill>-<kind>-<ts>.<ext>``.

        Skill name is sanitized to filesystem-safe characters
        (anything outside alnum / dash / underscore becomes ``-``)
        so skills with spaces / colons in their name still produce
        a path the OS will accept. Timestamped to avoid silently
        overwriting a previous save when the user runs several
        tests in a row and saves each one."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(
            c if c.isalnum() or c in "-_" else "-"
            for c in self._skill.name
        ) or "skill"
        return str(self._cwd / f"{safe}-{kind}-{ts}.{ext}")

    def _write_text_file(self, path: Path, text: str, *, kind_label: str) -> None:
        """Common writer used by both save paths. UTF-8 encoded,
        unconditional overwrite (the file dialog already prompted on
        clobber via the OS-native ``QFileDialog`` confirmation).
        Surfaces errors via ``QMessageBox.warning`` so failures
        don't disappear silently — the user picked a destination
        and deserves a clear verdict either way."""
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            _log(f"save failed: {path}: {e}")
            QMessageBox.warning(
                self, "Save failed",
                f"Could not save {kind_label} to:\n{path}\n\n{e}")
            return
        _log(f"saved {kind_label}: {path}")
        self.status_label.setText(f"Saved {kind_label} → {path.name}")

    def _on_cancel(self) -> None:
        """Cancel button (§7.43). Sets the flag and kills the
        subprocess; the worker thread's ``communicate()`` returns
        immediately after the kill, the worker emits
        ``_worker_result`` with whatever stdout/stderr was drained,
        and ``_on_worker_result`` labels the verdict as "Cancelled"."""
        if not self._is_running():
            return
        self._was_cancelled = True
        self.status_label.setText("Cancelling…")
        self._kill_subproc()

    def _on_timeout(self) -> None:
        """Hard timeout fired (§7.43). Same path as Cancel but sets
        ``_timed_out`` so the worker-result handler labels the
        verdict as "TIMED OUT" with troubleshooting hints."""
        if not self._is_running():
            return
        self._timed_out = True
        self.status_label.setText("Timing out — killing process…")
        self._kill_subproc()

    def _on_clear(self) -> None:
        """Reset both panes back to the initial state, and explicitly
        forget any captured Claude conversation session (§7.46). Clear
        is the user's "start over" gesture — it should reset *all*
        run-derived state so the next Run is as if the dialog had just
        opened. Toggling the Continue-conversation checkbox alone does
        NOT clear the session id; only this button does.

        Doesn't touch in-flight run state — the GUI thread isn't
        running anything synchronously here (the worker is on its own
        thread), but the button is enabled regardless and Clear during
        a run is harmless: it wipes panes but doesn't cancel."""
        self.prompt_edit.clear()
        self._clear_run_views()
        self._session_id = None
        self._update_session_label()
        self.status_label.setText("Idle")

    def _update_session_label(self) -> None:
        """Refresh the small ``(session: abc1234…)`` indicator next to
        the checkboxes. Truncates to the first 8 chars of the id —
        long enough to recognize across runs, short enough not to
        crowd the header row. Empty when no session is captured."""
        if self._session_id:
            self.session_label.setText(
                f"(session: {self._session_id[:8]}…)")
        else:
            self.session_label.setText("")

    def _teardown_process(self) -> None:
        """Stop timers, drop subprocess + worker thread references,
        restore the run-button state.

        Worker thread is **not** joined here — joining would block
        the GUI thread on a dead worker, and the worker is daemon
        so it can't outlive the process anyway. The
        thread / subproc references just go to ``None``; once any
        in-flight slots see that, they'll skip their bodies via
        ``_is_running()`` guards."""
        self._tick_timer.stop()
        self._timeout_timer.stop()
        with self._worker_lock:
            self._subproc = None
        self._worker_thread = None
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.run_busy.hide()

    # ---------------------------------------------------------- close handler
    def closeEvent(self, event) -> None:  # noqa: N802 — Qt naming
        """Kill any running subprocess, briefly join the worker
        thread (so we don't leak it), emit ``closed``, let Qt destroy
        the dialog (WA_DeleteOnClose).

        Join timeout is short — if the worker is stuck, the daemon
        flag lets it die naturally with the process; we don't want
        to block UI shutdown on a wedged child."""
        self._was_cancelled = True
        self._kill_subproc()
        if (self._worker_thread is not None
                and self._worker_thread.is_alive()):
            self._worker_thread.join(timeout=1.5)
        self.closed.emit(self._skill.path)
        super().closeEvent(event)


# ----------------------------------------------------------------- helpers

def _context_label(skill: Skill) -> str:
    """Plugin name (Plugin skills) or project folder name (Project
    skills); ``""`` for Global.

    Deliberately duplicated from
    :mod:`claude_skills_manager.ui.skill_list`'s module-private
    ``_context_label`` rather than imported. The seven lines of logic
    are simple and stable, the cross-module import would couple this
    dialog to the skill-list panel's internals, and the alternative
    (promoting to a public helper in a shared module) ripples three
    files for very little gain. If the rule ever grows to a fourth
    case (e.g., marketplace context for plugin skills with
    cross-marketplace name collisions), promote then."""
    if skill.type == SkillType.PLUGIN and skill.plugin_id:
        return skill.plugin_id.partition("@")[0]
    if skill.type == SkillType.PROJECT:
        try:
            return skill.path.parents[2].name
        except IndexError:
            return ""
    return ""


def _state_to_label(state: str) -> str:
    """Human-readable state name for the dialog header.

    The settings layer uses lowercase wire values (``on``, ``off``,
    ``name-only``, ``user-invocable-only``, plus the synthesized
    ``plugin-off``); the UI surfaces them as title-case labels with
    the plugin-off case spelled out so the inheritance is visible."""
    if state == STATE_ON:
        return "Enabled"
    if state == STATE_OFF:
        return "Disabled"
    if state == STATE_PLUGIN_OFF:
        return "Disabled (plugin off)"
    if state == STATE_NAME_ONLY:
        return "Name-only"
    if state == STATE_USER_INVOCABLE_ONLY:
        return "User-invocable only"
    return state


def _format_mtime(path: Path | None) -> str:
    """Format a file's mtime as ``YYYY-MM-DD HH:MM``, or a placeholder
    if the file can't be stat'd. Header strings can't fail to render —
    we want a short string in every case."""
    if path is None or not path.is_file():
        return "(unknown)"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "(unreadable)"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
