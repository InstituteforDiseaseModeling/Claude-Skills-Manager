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
    QAction, QFont, QKeySequence, QShortcut, QTextCursor, QTextOption,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSplitter, QStyle, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget,
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
    find_claude_executable, read_skill_md_text,
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
    # One emission per stream-json event line (§7.60). Payload is the
    # raw JSON text; the GUI slot parses it and dispatches by
    # ``type``. Queued connection auto-routes from the worker thread
    # to the GUI thread, same as the result/failed signals above —
    # but emission frequency is much higher (potentially tens per
    # second on tool-heavy runs), so the slot must be cheap.
    _worker_stream_event = Signal(str)

    # Stable tab indices — match the build order in `_build_ui`.
    # Description / Raw SKILL.md / Claude.
    _DESCRIPTION_TAB = 0
    _RAW_TAB = 1
    _TEST_TAB = 2

    # Stable inner-tab indices for the Run section's nested QTabWidget
    # (§7.44 + §7.60). Order is Response, Activity, Raw Output —
    # Response is the headline result and stays at index 0 so the
    # default tab on dialog open is unchanged; Activity is sandwiched
    # in the middle because it's "narrative", not "raw", and
    # surfacing it next to Response makes it discoverable; Raw Output
    # stays in its historical position-of-last-resort.
    _SUB_RESPONSE = 0
    _SUB_ACTIVITY = 1
    _SUB_RAW = 2

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
        # Effective hard-timeout for the *current* run (§7.59).
        # Snapshotted from settings at ``_on_run`` start and used by
        # ``_on_tick`` for the countdown display and by the TIMED OUT
        # verdict. The +30s / +60s / +5m extend buttons mutate this
        # value mid-run and re-start ``_timeout_timer`` with the new
        # remaining slice — but never write back to QSettings, so the
        # next Run (and the next dialog open) reads the user's
        # persisted Help → Settings… value unchanged.
        self._current_timeout_ms = 0
        # Multi-turn conversation state (§7.46 + §7.68). ``_session_id``
        # is the id Claude Code returned in the previous successful Run
        # within THIS dialog window; the next Run passes it back as
        # ``--resume <id>`` so claude restores the prior context.
        # Continue-mode is unconditional (the checkbox was removed),
        # so the id is consulted on every Run automatically — empty/
        # None means "no captured session yet" and the next Run starts
        # fresh.
        #
        # Lifetime: in-memory only, scoped to this dialog instance
        # (§7.68 reverses the §7.65 / §7.67 QSettings persistence).
        # WA_DeleteOnClose drops the state on dialog close, so every
        # fresh open of the Test Skill window starts a brand new
        # conversation — the user-stated semantic that supersedes the
        # earlier "remember across opens" design. Multi-turn within
        # one window is preserved (Run → capture → next Run continues).
        #
        # ``_session_cwd`` records the cwd at which the current
        # ``_session_id`` was captured. The Run boundary compares it
        # against the current ``self._cwd`` and drops the session if
        # they differ — Claude CLI scopes conversation history to the
        # cwd's project slug, so a session captured under cwd_A is
        # not resumable under cwd_B. Action-boundary check (no event
        # wiring on Browse / typing) keeps the composition with
        # §7.48's Working Directory control clean.
        self._session_id: str | None = None
        self._session_cwd: Path | None = None
        # Stream-event capture (§7.60). Stream-json is always-on for
        # the test runner, and the GUI captures the salient bits as
        # events fly past so ``_on_worker_result`` has a final
        # response/session_id/usage/error verdict ready without
        # re-parsing anything. All four are reset at the start of
        # every Run; reading them from the result-handler before a
        # Run has happened yields the empty/None defaults below,
        # which the verdict paths handle as "no stream data" and
        # fall back to raw stdout — a defensive but not load-bearing
        # branch (a Run that finished without a result event is the
        # crash case).
        self._stream_response_text: str = ""
        self._stream_session_id: str | None = None
        self._stream_is_error: bool = False
        self._stream_usage: dict | None = None
        self._stream_event_count: int = 0
        # Raw Output tab has two views (§7.61): a "pretty" CLI-style
        # transcript (default) and the verbatim JSON stream (kept
        # available for the schema-tolerant debug affordance from
        # §7.60 — never crash on unknown event shapes, but also: let
        # the user *see* the unknown shapes). Each is maintained as
        # a shadow string so flipping the toggle is just a setPlainText
        # off the active buffer. ``_append_raw_buf`` / ``_append_pretty_buf``
        # / ``_append_both`` keep the buffers and the visible widget
        # in sync; ``_on_raw_view_mode_changed`` handles the swap.
        # Non-event lines (preface, stderr, verdict, [timeout extended])
        # land in both buffers verbatim — those *are* the CLI-style
        # presentation already, so there's nothing to reformat.
        self._raw_buffer_text: str = ""
        self._pretty_buffer_text: str = ""
        self._raw_view_mode: str = "pretty"
        # Per-run state for tool_use → tool_result correlation in the
        # pretty formatter. Stream-json emits these as separate events
        # in order, so we stash the most-recent tool name when we see
        # a ``tool_use`` block and consume it on the next ``tool_result``
        # — turns the result line into ``  ⎿ Glob returned 839 chars``
        # instead of a bare ``  ⎿ (839 chars)`` with no tool context.
        # Reset in ``_clear_run_views`` alongside the other ``_stream_*``
        # captures.
        self._stream_last_tool_name: str = ""
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
        self._worker_stream_event.connect(self._on_stream_event)

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
        # Layout: label + editable path field + Browse button.
        # Field is editable: typing is a valid mutation path
        # alongside Browse, and a ``textChanged`` listener keeps
        # ``self._cwd`` in sync on every keystroke. Trade-off: the
        # user can type a malformed / non-existent path that
        # ``QFileDialog`` would have rejected. Validation moves
        # to the Run boundary — Popen raises on a missing cwd and
        # the existing ``_on_worker_failed`` path surfaces the
        # error message cleanly. Same idiom as a shell ``cd``.
        cwd_row = QHBoxLayout()
        self._cwd_label = QLabel("Working Directory:")
        cwd_row.addWidget(self._cwd_label)
        self.cwd_display = QLineEdit(str(self._cwd))
        self.cwd_display.setToolTip(
            "Directory `claude` will be invoked from for every Run "
            "in this window. Affects which project memory file "
            "(~/.claude/projects/<slug>/memory/MEMORY.md) and which "
            "project-local settings (.claude/settings.local.json) the "
            "subprocess loads. Type a path or click Browse to change.")
        # Live sync: every keystroke updates ``self._cwd`` so the
        # next Run uses whatever's currently in the field. Browse
        # also calls ``setText`` which routes through here; the
        # handler's equality short-circuit makes that a no-op.
        self.cwd_display.textChanged.connect(self._on_cwd_text_changed)
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
        self.trust_dir_display.setPlaceholderText(
            "(none — Read scoped to working directory)")
        self.trust_dir_display.setToolTip(
            "Additional directory `claude` will be granted Read on for "
            "every Run in this window (passed as --add-dir). "
            "Pre-populated with the selected skill's folder so prompts "
            "that reference SKILL.md by absolute path can read it. "
            "Type a path, click Browse to pick a directory, or click "
            "the × to limit Read to the working directory only.")
        self.trust_dir_display.setFont(cwd_font)
        self.trust_dir_display.textChanged.connect(
            self._on_trust_dir_text_changed)
        # Inline clear "×" — kept as a custom action rather than
        # Qt's built-in ``setClearButtonEnabled`` so we can give it
        # an explicit tooltip ("Clear Trust Directory…") that names
        # what's being cleared and what the next-Run consequence is.
        # The built-in shows the same glyph but offers no hover
        # explanation. Historical note: this field used to be
        # read-only, which would have *required* a custom action
        # anyway (Qt's built-in is gated by ``!isReadOnly()``); now
        # that the field accepts typing, the custom action is a
        # stylistic preference rather than a workaround.
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
            "Run time. Skipped when 'Skip Permission Prompts' is "
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

        # Header row: "Prompt:" label + Prefix Skill Name + Skip
        # Permission Prompts. The checkboxes sit on the same line as
        # the label so the relationship to the prompt below is
        # unambiguous (vs. floating them elsewhere). No session
        # indicator: continue-mode is unconditional and the id is
        # persisted silently in QSettings (§7.46) — the user doesn't
        # need to see it, and surfacing an opaque 8-char id was more
        # visual noise than payoff.
        prompt_header = QHBoxLayout()
        prompt_header.addWidget(QLabel("Prompt:"))
        prompt_header.addStretch(1)
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
        self.skip_perms_checkbox = QCheckBox("Skip Permission Prompts")
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

        # Stretch goes here, *before* the extend-timeout buttons, so
        # the buttons pin to the right edge of the run bar rather
        # than drifting with the status label's text length. Pattern:
        # ``[left-packed widgets] [stretch] [right-anchored widgets]``.
        # The trailing position was the source of the §7.60.x layout
        # bug — with the stretch at the end, status_label growing
        # pushed everything else (including the extend buttons)
        # right by however many pixels the status text gained.
        bar.addStretch(1)

        # Extend-timeout buttons (§7.59). Three fixed deltas — wide
        # enough to cover both "claude's a little slow today, give it
        # half a minute more" and "this is going to be a long run,
        # buy me five minutes". Hidden when no run is in progress;
        # show/hide pairs with ``run_busy`` so the visual cluster
        # ("busy + extend") moves as one. Per-run only — clicking
        # one mutates ``_current_timeout_ms`` and restarts the
        # backing QTimer; QSettings is never touched, so the next
        # Run reads the user's persisted Help → Settings… value.
        self._extend_buttons: list[QPushButton] = []
        for label, delta_s in (("+30s", 30), ("+60s", 60),
                               ("+5m", 300), ("+30m", 1800)):
            btn = QPushButton(label)
            btn.setStyleSheet(BUTTON_STYLE)
            btn.setToolTip(
                f"Add {delta_s} seconds to the timeout for this run "
                "only. Does not change the saved default in "
                "Help → Settings…")
            btn.hide()
            # ``d=delta_s`` captures the value at definition time —
            # without the default-argument idiom, Python's late-bound
            # closures would have all three buttons fire with the
            # last loop value (300). Same gotcha that bites
            # ``for i in range(N): callbacks.append(lambda: f(i))``.
            btn.clicked.connect(lambda _checked=False, d=delta_s:
                                self._extend_timeout(d))
            bar.addWidget(btn)
            self._extend_buttons.append(btn)

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
        _corner_btn_style = """
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
        """
        # Open File… — loads an external file into whichever inner
        # tab is currently visible:
        #   Response → .md (rendered via setMarkdown)
        #   Activity → .txt (rendered as plain text)
        #   Raw Output → .txt (rendered as plain text)
        # Same context-sensitive corner-widget rationale as Save As…;
        # placed first because Open is a precursor action (input)
        # while Save is a terminating action (output) — left-to-right
        # reads as the natural I/O flow.
        self._open_file_btn = QPushButton("Open File…")
        self._open_file_btn.setStyleSheet(_corner_btn_style)
        self._open_file_btn.setCursor(Qt.PointingHandCursor)
        self._open_file_btn.clicked.connect(self._on_open_file_clicked)

        self._save_as_btn = QPushButton("Save As…")
        self._save_as_btn.setStyleSheet(_corner_btn_style)
        self._save_as_btn.setCursor(Qt.PointingHandCursor)
        self._save_as_btn.clicked.connect(self._on_save_as_clicked)

        # Clear — per-tab destructive reset for the currently-visible
        # inner sub-tab only. Distinct from ``self.clear_btn`` next to
        # Run/Cancel (which is a wholesale "start over" — prompt +
        # all three panes + session id). This one wipes ONLY the tab
        # the user is looking at, leaving the other two intact. Same
        # context-sensitive corner-widget rationale as Save As… /
        # Open File…; placed last because Clear is the destructive
        # action and "input → output → reset" reads naturally
        # left-to-right.
        self._clear_tab_btn = QPushButton("Clear")
        self._clear_tab_btn.setStyleSheet(_corner_btn_style)
        self._clear_tab_btn.setCursor(Qt.PointingHandCursor)
        self._clear_tab_btn.clicked.connect(self._on_clear_tab_clicked)

        # Wrap all three buttons in a container — Qt's setCornerWidget
        # takes one widget per corner, so a thin QHBoxLayout-on-
        # QWidget pair is the canonical way to host multiple
        # controls in a corner slot. Margins kept at zero so the
        # buttons align flush with the tab strip's right edge.
        corner_host = QWidget()
        corner_layout = QHBoxLayout(corner_host)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(4)
        corner_layout.addWidget(self._open_file_btn)
        corner_layout.addWidget(self._save_as_btn)
        corner_layout.addWidget(self._clear_tab_btn)
        self._response_tabs.setCornerWidget(
            corner_host, Qt.TopRightCorner)
        # Re-evaluate enabled state and tooltips whenever the user
        # switches between Response / Activity / Raw Output — all
        # three corner buttons describe whichever tab is currently
        # visible. ``_update_save_btn_state`` propagates to Clear
        # too (the two enable rules are identical: tab has content
        # AND we aren't running), so wiring just the Save handler
        # is enough.
        self._response_tabs.currentChanged.connect(
            self._update_save_btn_state)
        self._response_tabs.currentChanged.connect(
            self._update_open_btn_tooltip)

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
        # Wrap long content rather than scroll horizontally. Two
        # complementary settings:
        #   - ``setWordWrapMode(WrapAtWordBoundaryOrAnywhere)``
        #     tells the QTextEdit block layout engine that
        #     unbreakable tokens (long URLs, dense identifiers)
        #     can wrap mid-token rather than overflow.
        #   - The default-style-sheet ``pre`` rule fixes fenced
        #     code blocks: ``setMarkdown`` emits them as ``<pre>``,
        #     which Qt renders with ``white-space: pre`` by default
        #     (preserve whitespace, no wrap). ``pre-wrap`` keeps
        #     the formatting indentation but allows soft-wraps at
        #     spaces; ``break-word`` is the fallback for code
        #     lines with no spaces (e.g., huge URLs in a code
        #     block) so they wrap rather than push the scrollbar.
        # Same fix shape applies anywhere ``setMarkdown`` is used
        # with content that might contain long code lines.
        self.response_view.setWordWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.response_view.document().setDefaultStyleSheet(
            "pre { white-space: pre-wrap; word-wrap: break-word; }")
        self._response_tabs.addTab(self.response_view, "Response")

        # ---- Tab 2: Activity (live progress / steps — §7.60) ----
        # Stream-json events from ``claude --output-format stream-json
        # --verbose`` are parsed by ``_on_stream_event`` and appended
        # here as a chronological narrative: session init, assistant
        # text deltas, tool calls with their inputs, tool results
        # with their sizes, terminal "Done" verdict with usage stats.
        # ``QTextBrowser`` rather than ``QPlainTextEdit`` because
        # event lines benefit from light HTML formatting (dim
        # timestamps, bold tool names, color-coded glyphs) — same
        # widget Family as Response, different content shape.
        # Read-only.
        self.activity_view = QTextBrowser()
        self.activity_view.setOpenExternalLinks(False)
        self.activity_view.setPlaceholderText(
            "(Live activity — tool calls, tool results, assistant "
            "text — will appear here as the run progresses)")
        self._response_tabs.addTab(self.activity_view, "Activity")

        # ---- Tab 3: Raw Output (chronological monospace) ----
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

        # Two-mode container (§7.61). The Raw Output tab carries both
        # the CLI-style "Pretty" transcript (default) and the
        # verbatim JSON "Raw JSON" stream; the combobox swaps the
        # visible widget contents between the two internal buffers.
        # The text widget itself is shared — only its content changes
        # — so cursor / font / scroll behavior is identical across
        # modes and the existing save / load / clear plumbing keeps
        # working without indirection.
        raw_container = QWidget()
        raw_layout = QVBoxLayout(raw_container)
        raw_layout.setContentsMargins(0, 0, 0, 0)
        raw_layout.setSpacing(4)

        raw_toolbar = QHBoxLayout()
        raw_toolbar.setContentsMargins(6, 4, 6, 0)
        raw_toolbar_label = QLabel("View:")
        raw_toolbar.addWidget(raw_toolbar_label)
        self.raw_view_mode_combo = QComboBox()
        self.raw_view_mode_combo.addItem("Pretty (CLI-style)", "pretty")
        self.raw_view_mode_combo.addItem("Raw JSON", "raw")
        self.raw_view_mode_combo.setToolTip(
            "Pretty: human-readable transcript like the Claude Code "
            "terminal output.\n"
            "Raw JSON: the verbatim stream-json events, one per line "
            "— useful for debugging unfamiliar event shapes.")
        self.raw_view_mode_combo.currentIndexChanged.connect(
            self._on_raw_view_mode_changed)
        raw_toolbar.addWidget(self.raw_view_mode_combo)
        raw_toolbar.addStretch(1)
        raw_layout.addLayout(raw_toolbar)
        raw_layout.addWidget(self.raw_view, 1)

        self._response_tabs.addTab(raw_container, "Raw Output")

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
            self._open_file_btn.setFixedHeight(bar_height)
            self._clear_tab_btn.setFixedHeight(bar_height)

        # Initial Save As… state: all three panes are empty at
        # construct, so the button starts disabled with a "run
        # something first" tooltip. Subsequent renders / appends /
        # clears refresh it.
        self._update_save_btn_state()
        # Open File… is always enabled when not running; seed its
        # tooltip to describe whichever tab is current at construct.
        self._update_open_btn_tooltip()

    # ---- Working-directory control -----------------------------------------
    def _on_cwd_text_changed(self, text: str) -> None:
        """Keep ``self._cwd`` in sync with whatever's in the Working
        Directory field. Fires on every keystroke (typing) AND on
        the programmatic ``setText`` from :meth:`_on_browse_cwd`.

        Empty / whitespace-only text is treated as "user is mid-edit"
        — we leave ``self._cwd`` at its previous value rather than
        coerce ``Path("")`` (which resolves to ``Path(".")``) into the
        runner. The user can clear the field and type a new path
        without the dialog briefly committing the current process
        cwd in between.

        Validation is deliberately deferred to Run time: Popen
        raises on a missing cwd, and the existing
        :meth:`_on_worker_failed` path surfaces a clean error
        message. Mirroring the comment on the row above (and the
        "shell ``cd``" idiom), typing a path you can't ``cd`` into
        is a Run-time failure, not a typing-time veto.

        Idempotent against Browse: ``_on_browse_cwd`` sets
        ``self._cwd`` *before* calling ``setText``, so by the time
        this handler runs the equality check short-circuits."""
        stripped = text.strip()
        if not stripped:
            return
        new_cwd = Path(stripped)
        if new_cwd == self._cwd:
            return
        _log(f"working directory typed: {self._cwd} -> {new_cwd}")
        self._cwd = new_cwd

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
        # "Skip Permission Prompts" checkbox is still an escape hatch.
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
        and keep ``self._trust_dir`` in lockstep with the field on
        every mutation — typing, Browse, or the inline clear "×".

        ``textChanged`` fires for every text mutation: programmatic
        ``setText`` from :meth:`_on_browse_trust_dir`, the inline
        clear action's slot calling ``QLineEdit.clear()``, AND every
        keystroke now that the field is editable. We do three cheap
        things on each fire:

        1. Toggle the action icon's visibility (so it appears only
           when there's text to clear).
        2. On empty / whitespace text: reset ``self._trust_dir`` to
           ``None`` so the next Run omits ``--add-dir``. Whitespace
           is treated as "no path" — same shape as cwd typing
           tolerance.
        3. On non-empty text: set ``self._trust_dir`` to the
           ``Path`` parsed from the input. Validation deferred to
           Run time, mirroring :meth:`_on_cwd_text_changed`.

        Browse path stays idempotent: it sets ``self._trust_dir``
        before calling ``setText``, so the equality short-circuit
        keeps the log clean."""
        # Guard against this firing before _build_ui finishes wiring
        # the action (the QLineEdit's initial text triggers no signal,
        # so in practice this is just belt-and-braces).
        if hasattr(self, "_trust_clear_action"):
            self._trust_clear_action.setVisible(bool(text))
        stripped = text.strip()
        if not stripped:
            if self._trust_dir is None:
                return  # already None — nothing to log or sync
            _log(f"trust directory cleared (was {self._trust_dir})")
            self._trust_dir = None
            return
        new_dir = Path(stripped)
        if self._trust_dir == new_dir:
            return  # programmatic Browse → setText already in sync
        _log(f"trust directory typed: {self._trust_dir} -> {new_dir}")
        self._trust_dir = new_dir

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

        Decoupled from "Skip Permission Prompts" deliberately. Trust
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
                f"'Skip Permission Prompts'.",
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

            # Action-boundary validation of the directory fields.
            # The textChanged handlers intentionally don't validate
            # per-keystroke (see ``_on_cwd_text_changed`` docstring);
            # this is the one place the paths are checked before
            # they're handed to Popen / --add-dir. ``is_dir()``
            # catches both "missing" and "points at a file" in one
            # call. Trust Directory is optional — ``None`` means no
            # ``--add-dir`` grant, which is a valid choice — so it's
            # only validated when actually set.
            #
            # Surfaced via ``QMessageBox.warning`` only — the modal
            # is the visible alert; no status_label breadcrumb is
            # needed (the user has already acknowledged the dialog).
            if not self._cwd.is_dir():
                _log(f"run aborted: cwd does not exist: {self._cwd}")
                QMessageBox.warning(
                    self, "Working Directory not found",
                    f"Working Directory does not exist:\n\n"
                    f"{self._cwd}\n\n"
                    f"Fix the path and try again.")
                return
            if (self._trust_dir is not None
                    and not self._trust_dir.is_dir()):
                _log(
                    "run aborted: trust dir does not exist: "
                    f"{self._trust_dir}")
                QMessageBox.warning(
                    self, "Trust Directory not found",
                    f"Trust Directory does not exist:\n\n"
                    f"{self._trust_dir}\n\n"
                    f"Fix the path or clear it.")
                return

            # Prefix Skill Name checkbox is a *prompt-editing
            # shortcut*, NOT a send-time invariant: toggling it
            # adds / removes the ``/<skill> `` token in the prompt
            # textbox (see ``_on_prefix_toggled``), and Run sends
            # whatever is currently in the textbox verbatim. The
            # user owns the prompt — if they ticked the box and
            # then manually deleted the prefix, that's an explicit
            # choice and Run respects it. (Earlier versions
            # re-enforced the checkbox state at this point, which
            # silently put the prefix back; that surprised users
            # who'd manually removed it.)

            # Pull live values from Settings on each run. A Settings
            # dialog change therefore applies to the very next click of
            # Run, without reopening this dialog.
            model = app_settings.get_model()
            api_key = app_settings.get_api_key()
            # Continue-mode is unconditional (§7.46). Resume id is
            # included whenever we have a session id from a prior turn
            # in THIS dialog instance; the first Run has no id yet and
            # runs fresh. Dialog close drops the in-memory state, so
            # the very next open is also fresh (§7.68). ``Clear`` is
            # the explicit gesture to forget the session mid-dialog —
            # there is no per-run opt-out (the checkbox this used to
            # live behind was removed once the default was deemed
            # strong enough to not need an escape hatch).
            #
            # Action-boundary cwd check (§7.68): a session_id is only
            # valid under the cwd it was captured at — Claude CLI
            # scopes conversation history to the cwd's project slug,
            # and reusing an id across cwds errors out with "No
            # conversation found with session ID …". When the user
            # has changed cwd since the last capture (via Browse or
            # by typing into the Working Directory field), drop the
            # stale session here so the next ``--resume`` argument is
            # built from a None state — i.e. omitted entirely.
            if (self._session_id is not None
                    and self._session_cwd is not None
                    and self._cwd.resolve()
                    != self._session_cwd.resolve()):
                _log(f"  dropping session {self._session_id} "
                     f"captured at {self._session_cwd} "
                     f"(current cwd {self._cwd} differs)")
                self._session_id = None
                self._session_cwd = None
            resume_id = self._session_id or ""
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
                # Stream-json is always on for the test runner
                # (§7.60). The Activity tab consumes events as they
                # arrive; session_id, response markdown, and usage
                # stats are extracted from stream events in
                # ``_on_stream_event`` and stashed for the verdict
                # handler. The plain JSON envelope mode this used
                # to take (when continue mode was on) is now
                # subsumed — stream-json's terminal ``result``
                # event carries everything the envelope did.
                stream_json=True,
                skip_permissions=skip_perms,
                extra_read_dirs=extra_read_dirs,
            )
            env_overrides = claude_env_overrides(api_key)
            timeout_ms = _test_run_timeout_ms()
            # Snapshot for this run only (§7.59). Extend buttons
            # mutate ``self._current_timeout_ms`` mid-run; the tick
            # display and the TIMED OUT verdict read from the snapshot
            # so they stay coherent with what the timer will actually
            # do.
            self._current_timeout_ms = timeout_ms
            timeout_s = timeout_ms / 1000

            _log("=" * 60)
            _log(f"_on_run START")
            _log(f"  prompt: {prompt!r}")
            _log(f"  cmd: {cmd!r}")
            _log(f"  model: {model!r}  (empty = let claude pick)")
            _log(f"  api_key override: {'yes' if api_key else 'no'}")
            _log(f"  resume_id: "
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
            # Reset stream-event capture (§7.60). Done HERE in
            # ``_on_run`` rather than inside ``_clear_run_views``
            # because ``_clear_run_views`` is also called from the
            # user-facing Clear button, where wiping the
            # session_id and response text would silently lose the
            # captured-from-last-run resume id. Per-Run reset is
            # the right scope.
            self._stream_response_text = ""
            self._stream_is_error = False
            self._stream_usage = None
            self._stream_event_count = 0
            # NB: don't reset _stream_session_id here — system/init
            # will overwrite it on the next stream, and clearing it
            # before that arrival would race with continue-mode
            # capture across Runs.
            if resume_id:
                self.status_label.setText(
                    f"Resuming session {resume_id[:8]}…")
            else:
                self.status_label.setText("Starting…")
            self.run_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)
            # Mid-Run the worker is actively appending to Activity /
            # Raw Output; letting the user load an external file
            # would clobber the live stream and the next event would
            # then layer on top of the loaded content. Disable until
            # _finish_run flips it back. Save As… stays available
            # because saving in-flight content is sometimes useful.
            self._open_file_btn.setEnabled(False)
            # Per-tab Clear is gated mid-Run by
            # ``_update_clear_tab_btn_state`` (it short-circuits on
            # ``_is_running()``), but we also disable explicitly here
            # so the visual flip happens at the action boundary, not
            # whenever the next ``_update_save_btn_state`` happens to
            # fire. Same reasoning as the Open File button above.
            self._clear_tab_btn.setEnabled(False)
            self.run_busy.show()
            for btn in self._extend_buttons:
                btn.show()
            # Surface the live event stream (§7.60) as soon as Run is
            # clicked — Activity is where new content arrives during a
            # run, so auto-selecting it spares the user the manual tab
            # click. Mirrored by the switch back to Response in
            # ``_teardown_process`` once the run finishes.
            self._response_tabs.setCurrentIndex(self._SUB_ACTIVITY)

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
            # Stream-json + verbose are always emitted now (§7.60).
            # Surface them in the diagnostic preface so the shell
            # copy/paste lines up with what actually ran.
            diag_lines.append(
                "  --output-format stream-json --verbose  "
                "(live activity stream)")
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
            self._append_both("\n".join(diag_lines))

            # Pretty-only prompt prefix line — mirrors Claude Code's
            # terminal convention where the user prompt appears as a
            # ``> ...`` header before the assistant's narrative. Adds
            # context to the Pretty view without polluting the Raw
            # JSON view (which already shows the prompt as a CLI arg
            # in the diagnostic preface).
            self._append_pretty_buf(f"> {prompt}\n\n")

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
            self._append_both(
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
                # Line-buffered so the for-line iterator below
                # surfaces each newline as soon as the child flushes
                # it — required for the live Activity tab to show
                # events as they happen rather than at process exit.
                bufsize=1,
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

        # ---- Streaming read (§7.60) ----
        # ``communicate()`` is replaced with a line-by-line iterator
        # so the GUI can render events as they arrive. We still need
        # to accumulate both streams in full so ``_worker_result``
        # gets the same payload contract as the pre-stream code
        # path (the verdict handler reads ``stdout`` as a fallback
        # when no ``result`` event was captured).
        #
        # Stderr is drained on a sidecar thread to prevent the
        # classic pipe-buffer deadlock: if claude wrote a few MB to
        # stderr while we were only reading stdout, its stderr-side
        # write() would block, claude would stop emitting on stdout,
        # and we'd hang forever waiting for the next stdout line.
        # The sidecar drains concurrently into a local list.
        stdout_accum: list[str] = []
        stderr_accum: list[str] = []

        def _drain_stderr() -> None:
            try:
                for sline in proc.stderr:
                    stderr_accum.append(sline)
            except Exception:
                # Pipe closed unexpectedly (process killed mid-write)
                # — the main thread will pick up the exit code; the
                # partial stderr we got is good enough.
                pass

        stderr_thread = threading.Thread(
            target=_drain_stderr,
            daemon=True,
            name="ClaudeTestSkillStderr",
        )
        stderr_thread.start()
        _log("[worker] stderr drainer thread started")

        try:
            for line in proc.stdout:
                stdout_accum.append(line)
                # Emit per-line. Qt's QueuedConnection serializes
                # delivery on the GUI thread's event loop, so a
                # burst of events stays in order even if the GUI
                # is briefly busy.
                self._worker_stream_event.emit(line)
        except Exception:
            tb = traceback.format_exc()
            _log(f"[worker] stdout iter raised:\n{tb}")
            # Don't bail out — proc.wait() below still needs to run
            # so we can collect the exit code. The accumulated
            # partial stdout is reported in _worker_result.

        # Wait for the process to actually exit and the stderr
        # drainer to finish. Short stderr join timeout (the drainer
        # exits when the stderr pipe closes, which the kernel does
        # right after the child exits).
        try:
            exit_code = proc.wait()
        except Exception:
            tb = traceback.format_exc()
            _log(f"[worker] proc.wait() raised:\n{tb}")
            self._worker_failed.emit(
                f"proc.wait() raised:\n{tb}")
            return
        stderr_thread.join(timeout=1.0)

        _log(f"[worker] streaming done: exit={exit_code}, "
             f"stdout_lines={len(stdout_accum)}, "
             f"stderr_lines={len(stderr_accum)}")
        self._worker_result.emit(
            exit_code,
            "".join(stdout_accum),
            "".join(stderr_accum),
        )
        _log("[worker] _worker_main END")

    def _on_worker_result(
        self, exit_code: int, stdout: str, stderr: str,
    ) -> None:
        """``_worker_result`` slot — runs in the GUI thread (Qt
        auto-routes from the worker via ``QueuedConnection``).
        Paints output, classifies the verdict, restores button state."""
        _log(f"[gui] _on_worker_result: exit={exit_code}, "
             f"stdout={len(stdout)} chars, stderr={len(stderr)} chars")

        # Raw Output already has every stream event appended verbatim
        # via ``_on_stream_event`` (§7.60). No need to re-dump
        # ``stdout`` here — doing so would duplicate every line.
        # The stderr drain still needs to be surfaced, though.
        if stderr.strip():
            self._append_both(f"\n[stderr]\n{stderr}")
            if not stderr.endswith("\n"):
                self._append_both("\n")

        duration = 0.0
        if self._run_started_at is not None:
            duration = time.monotonic() - self._run_started_at
        self._received_bytes = len(stdout) + len(stderr)

        if self._timed_out:
            # Per-run snapshot (§7.59) — so the printed limit
            # reflects any extensions the user clicked, not the
            # original settings value at run start.
            limit = self._current_timeout_ms // 1000
            self.status_label.setText(
                f"TIMED OUT after {duration:.1f}s (limit {limit}s)")
            self._append_both(
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
            self._append_both(
                f"\n[cancelled by user after {duration:.1f}s]\n")
            self._set_response_markdown(
                f"*Cancelled by user after {duration:.1f}s.*")
        elif exit_code != 0:
            self.status_label.setText(
                f"Exit {exit_code} after {duration:.1f}s")
            self._append_both(
                f"\n[exited with code {exit_code} "
                f"after {duration:.1f}s]\n")
            # Prefer the stream-captured response if a ``result``
            # event arrived before claude exited; some failure modes
            # (rate limit, transient API error) emit a result event
            # *and* a non-zero exit code, and the result text is
            # what the user actually wants to read. Falls back to a
            # generic notice when no useful text was captured —
            # dumping the raw event-line stream as markdown would
            # render as unreadable JSON soup.
            if self._stream_response_text:
                self._set_response_markdown(
                    self._stream_response_text)
            else:
                self._set_response_markdown(
                    f"**`claude` exited with code {exit_code} "
                    f"after {duration:.1f}s and produced no final "
                    f"answer.** See the **Activity** and **Raw "
                    f"Output** tabs for what was emitted before exit.")
        else:
            # Happy path. Stream-json terminal ``result`` event has
            # already populated ``_stream_response_text`` with the
            # rendered markdown response, ``_stream_session_id``,
            # ``_stream_usage``, and ``_stream_is_error``. Use
            # those when present; fall back to raw stdout only if
            # the stream ended without a ``result`` event (e.g.,
            # claude crashed mid-stream — already handled as
            # exit_code != 0 above, but defensive coverage here
            # too).
            response_text = self._stream_response_text or stdout
            # Capture session_id eagerly so the next Run *within this
            # dialog* can resume. In-memory only (§7.68 reverses §7.65
            # / §7.67 cross-open persistence) — closing the window
            # drops the id, so a fresh open is always a fresh
            # conversation. ``_session_cwd`` is paired with the id so
            # the next Run's action-boundary cwd check can detect a
            # mid-dialog cwd change and drop the (now invalid) id
            # before it ever reaches ``--resume``.
            if self._stream_session_id:
                self._session_id = self._stream_session_id
                self._session_cwd = self._cwd
                _log(f"  captured session_id: "
                     f"{self._stream_session_id} "
                     f"(cwd: {self._session_cwd})")
            if self._stream_is_error:
                response_text = (
                    "**Claude reported an error for this turn.**\n\n"
                    + response_text)
            # Prefer real usage counts from the result event over
            # the char-based heuristic (§7.60). Output tokens are
            # what the user usually wants in the verdict; if
            # unavailable, fall back to the heuristic.
            usage_out = None
            if isinstance(self._stream_usage, dict):
                u_out = self._stream_usage.get("output_tokens")
                if isinstance(u_out, int):
                    usage_out = u_out
            if usage_out is None:
                usage_out = estimated_token_count(response_text)
                tokens_label = f"≈{usage_out:,} tokens"
            else:
                tokens_label = f"{usage_out:,} tokens"
            self._append_both(
                f"\n[done in {duration:.1f}s · "
                f"{self._received_bytes:,} chars total · "
                f"{self._stream_event_count} events]\n")
            # Status-bar verdict is intentionally session-agnostic —
            # the captured id is persisted silently per-skill in
            # QSettings, so surfacing "session abc12345…" here would
            # add visual noise without giving the user anything
            # actionable. Continuation across Runs (and across dialog
            # reopens for the same skill) Just Works.
            self.status_label.setText(
                f"Done in {duration:.1f}s · {tokens_label} out")
            self._set_response_markdown(response_text)

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
            self._append_both(
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
            self._append_both(f"\n[error] {error_msg}\n")
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
        # Read from the per-run snapshot, not the settings store
        # (§7.59). The extend buttons mutate this in place; the
        # settings value only seeds it at run start.
        timeout_s = self._current_timeout_ms / 1000
        remaining = max(0.0, timeout_s - elapsed)
        self.status_label.setText(
            f"Running… {elapsed:.1f}s · waiting for response "
            f"(timeout in {remaining:.0f}s)")

    def _on_stream_event(self, line: str) -> None:
        """Slot for ``_worker_stream_event`` (§7.60).

        Receives one raw JSON line from the worker thread (auto-
        marshalled by Qt's queued connection), parses it, dispatches
        by ``type`` to update the Activity tab, status label, and
        the captured-state instance fields (response text /
        session_id / usage / is_error) that ``_on_worker_result``
        reads at run end.

        Robustness contract: never raises. A malformed line shouldn't
        cancel the rest of the stream — log it as a `(unparsed)`
        activity entry and move on. An unknown ``type:`` shouldn't
        crash the parser — log it as `(unknown event: <type>)` and
        move on. The Raw Output tab still receives every line
        verbatim, so the user can debug from the literal stream even
        if our formatter doesn't recognize the shape."""
        line = line.rstrip("\r\n")
        if not line:
            return
        # Mirror to the Raw JSON buffer verbatim — preserves the
        # chronological diagnostic trail and keeps the
        # schema-tolerant debug affordance (§7.60): every line that
        # crossed the pipe is recoverable, even if our pretty
        # formatter doesn't recognize the shape. The CLI-style
        # pretty buffer gets its rendering from the per-event
        # handlers below.
        self._append_raw_buf(line + "\n")
        # Parse. JSON failure → activity entry + return; downstream
        # readers cope with the missing fields.
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            self._append_activity_html(
                self._fmt_ts(),
                "⚠",
                f"<span style='color:#a00;'>unparsed: "
                f"{html.escape(line[:120])}</span>",
            )
            return
        if not isinstance(event, dict):
            return
        self._stream_event_count += 1
        event_type = event.get("type")
        # Dispatch. Each branch can both update Activity (visible
        # narrative) and stash data into the ``_stream_*`` fields
        # (consumed by ``_on_worker_result``). Status label gets the
        # latest short summary so the user sees what claude is
        # currently doing — overrides the elapsed-time text from
        # ``_on_tick`` until the next tick fires (at most 500ms
        # later, by which point another event has usually arrived).
        if event_type == "system":
            self._handle_stream_system(event)
        elif event_type == "assistant":
            self._handle_stream_assistant(event)
        elif event_type == "user":
            self._handle_stream_user(event)
        elif event_type == "result":
            self._handle_stream_result(event)
        else:
            # Unknown event types are logged but don't break the
            # parser. ``claude``'s stream-json schema may grow new
            # event kinds across CLI versions; tolerating unknowns
            # is what keeps us forward-compatible.
            self._append_activity_html(
                self._fmt_ts(),
                "·",
                f"<span style='color:#888;'>event: "
                f"{html.escape(str(event_type))}</span>",
            )

    def _handle_stream_system(self, event: dict) -> None:
        """``type=system`` event — usually ``subtype=init`` with the
        initial session_id + model. We capture session_id eagerly
        (harmless when continue-mode is off; the field is only read
        on Continue runs) so resume works even if the user toggles
        the checkbox between runs."""
        subtype = event.get("subtype", "")
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._stream_session_id = session_id
        if subtype == "init":
            model = event.get("model") or "?"
            short_session = (session_id or "?")[:8]
            summary = (
                f"<b>session {html.escape(short_session)}…</b> "
                f"<span style='color:#666;'>"
                f"(model: {html.escape(str(model))})</span>")
            self._append_activity_html(self._fmt_ts(), "▸", summary)
            self._set_status_step(f"Session {short_session}… started")
            # Pretty: one-line session header. Matches the CLI
            # terminal's per-run banner shape — short session id +
            # model, no timestamp (the diagnostic preface above
            # already carries the launch timing).
            self._append_pretty_buf(
                f"▸ session {short_session}… · model: {model}\n\n")
        else:
            # Activity surfaces every system sub-event so the
            # debugging trail is complete. Pretty stays quiet for
            # non-init system events (hook_started / hook_response
            # etc.) — the CLI terminal doesn't surface them either,
            # and they'd clutter the transcript. Users who want them
            # can flip the toggle to Raw JSON.
            self._append_activity_html(
                self._fmt_ts(), "·",
                f"<span style='color:#888;'>system: "
                f"{html.escape(str(subtype))}</span>")

    def _handle_stream_assistant(self, event: dict) -> None:
        """``type=assistant`` event — the model emitted a message.
        Content is a list of blocks; ``text`` blocks contribute to
        the assistant's reply (which we accumulate for fallback
        rendering), ``tool_use`` blocks are calls into Read/Bash/etc.
        whose inputs we surface so the user can see what claude is
        about to do."""
        message = event.get("message") or {}
        content = message.get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                if isinstance(text, str) and text:
                    excerpt = text.strip().splitlines()[0][:140]
                    if len(text) > 140 or "\n" in text:
                        excerpt += "…"
                    self._append_activity_html(
                        self._fmt_ts(), "💭",
                        f"<span style='color:#1a3a5c;'>"
                        f"{html.escape(excerpt)}</span>")
                    self._set_status_step(
                        f"Assistant: {excerpt[:60]}…")
                    # Pretty: full assistant text (multi-line OK), capped
                    # at 2000 chars so a runaway block doesn't dominate
                    # the transcript. Activity's first-line truncation is
                    # deliberately stricter — it's the scannable index;
                    # Pretty is the readable narrative.
                    self._append_pretty_buf(
                        f"⏺ {self._pretty_clip(text.strip(), 2000)}\n\n")
            elif btype == "tool_use":
                tool_name = block.get("name") or "?"
                tool_input = block.get("input") or {}
                args_label = self._summarize_tool_input(tool_input)
                self._append_activity_html(
                    self._fmt_ts(), "🔧",
                    f"<b>{html.escape(str(tool_name))}</b>"
                    + (f"  <span style='color:#444;'>"
                       f"{html.escape(args_label)}</span>"
                       if args_label else ""))
                self._set_status_step(f"Calling {tool_name}…")
                # Pretty: tool call line in CLI shape —
                # ``⏺ ToolName(key: "v1", key: "v2")``. Stash the
                # tool name on the dialog so the next tool_result
                # event (which doesn't repeat the name in its own
                # payload) can correlate back to this call.
                pretty_args = self._pretty_format_tool_input(tool_input)
                self._stream_last_tool_name = str(tool_name)
                if pretty_args:
                    self._append_pretty_buf(
                        f"⏺ {tool_name}({pretty_args})\n")
                else:
                    self._append_pretty_buf(f"⏺ {tool_name}()\n")

    def _handle_stream_user(self, event: dict) -> None:
        """``type=user`` event — usually the tool_result that
        followed an assistant ``tool_use``. We surface the size +
        error flag so the user sees the round-trip without staring
        at multi-kilobyte tool outputs inlined in the Activity log."""
        message = event.get("message") or {}
        content = message.get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            is_error = bool(block.get("is_error", False))
            result = block.get("content")
            # ``content`` can be a string or a list of {type,text}
            # blocks depending on the tool. Coerce to a length number
            # for the summary; the literal content is already in Raw
            # Output for users who want to inspect it.
            if isinstance(result, str):
                size = len(result)
            elif isinstance(result, list):
                size = sum(
                    len(b.get("text", "")) if isinstance(b, dict)
                    else 0
                    for b in result)
            else:
                size = 0
            if is_error:
                self._append_activity_html(
                    self._fmt_ts(), "✗",
                    f"<span style='color:#a00;'>"
                    f"tool errored ({size:,} chars)</span>")
                self._set_status_step("Tool errored")
            else:
                self._append_activity_html(
                    self._fmt_ts(), "✓",
                    f"<span style='color:#2a6f2a;'>"
                    f"tool result ({size:,} chars)</span>")
                self._set_status_step(
                    f"Got tool result ({size:,} chars)")
            # Pretty: ``  ⎿ <excerpt>`` indented continuation line
            # under the previous tool call, ``Error:`` prefix on
            # is_error. Matches the Claude Code terminal convention
            # of showing a one-line peek at the result so the
            # transcript reads as a coherent narrative without
            # forcing the user into Raw JSON for context. Trailing
            # blank line separates the call/result pair from the
            # next assistant turn.
            excerpt = self._pretty_format_tool_result_content(result)
            if is_error:
                if excerpt:
                    self._append_pretty_buf(
                        f"  ⎿ Error: {excerpt} ({size:,} chars)\n\n")
                else:
                    self._append_pretty_buf(
                        f"  ⎿ Error ({size:,} chars)\n\n")
            else:
                if excerpt:
                    self._append_pretty_buf(
                        f"  ⎿ {excerpt} ({size:,} chars)\n\n")
                else:
                    self._append_pretty_buf(
                        f"  ⎿ ({size:,} chars)\n\n")
            self._stream_last_tool_name = ""

    def _handle_stream_result(self, event: dict) -> None:
        """``type=result`` event — terminal envelope. Carries the
        rendered assistant response (``result`` field), final
        session_id, usage stats, and the is_error flag. Stash into
        instance fields for ``_on_worker_result`` to consume."""
        result_text = event.get("result")
        if isinstance(result_text, str):
            self._stream_response_text = result_text
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._stream_session_id = session_id
        usage = event.get("usage")
        if isinstance(usage, dict):
            self._stream_usage = usage
        self._stream_is_error = bool(event.get("is_error", False))
        subtype = event.get("subtype", "")
        # Per-run usage / turn / duration bits — used by both the
        # Activity HTML and the Pretty plain-text rendering below.
        # Pulled here once so the two branches stay in sync (one
        # source of truth for "what numbers does this run report").
        usage_bits: list[str] = []
        if isinstance(usage, dict):
            out_t = usage.get("output_tokens")
            in_t = usage.get("input_tokens")
            if isinstance(out_t, int):
                usage_bits.append(f"out {out_t:,}t")
            if isinstance(in_t, int):
                usage_bits.append(f"in {in_t:,}t")
        num_turns = event.get("num_turns")
        if isinstance(num_turns, int):
            usage_bits.append(f"{num_turns} turn"
                              + ("s" if num_turns != 1 else ""))
        duration_ms = event.get("duration_ms")
        if isinstance(duration_ms, int):
            usage_bits.append(f"{duration_ms / 1000:.1f}s")
        if self._stream_is_error:
            self._append_activity_html(
                self._fmt_ts(), "✗",
                f"<span style='color:#a00;'>result: "
                f"{html.escape(str(subtype))}</span>")
            # Pretty: terminal error line mirroring CLI shape.
            # Subtype is the CLI's machine-readable label (e.g.
            # ``error_max_turns``); we surface it verbatim — the
            # user can look it up in claude's docs.
            sub_label = subtype or "error"
            self._append_pretty_buf(f"⏺ Error · {sub_label}\n")
        else:
            usage_str = (
                " <span style='color:#666;'>(" + " · ".join(usage_bits)
                + ")</span>" if usage_bits else "")
            self._append_activity_html(
                self._fmt_ts(), "✓",
                f"<b>Done</b>{usage_str}")
            # Pretty: terminal done line —
            # ``⏺ Done · out 3,551t · in 12t · 7 turns · 61.2s``.
            # Same bits as the Activity render but laid out for
            # monospace reading.
            if usage_bits:
                self._append_pretty_buf(
                    f"⏺ Done · {' · '.join(usage_bits)}\n")
            else:
                self._append_pretty_buf("⏺ Done\n")

    def _summarize_tool_input(self, tool_input: dict) -> str:
        """Render a tool call's input dict as a one-line label,
        prioritizing the fields users care about (file_path,
        command, pattern) and truncating long values so the Activity
        tab stays scannable.

        Not a pretty-printer — for the *literal* input the user can
        consult the Raw Output tab. This is the headline label."""
        if not isinstance(tool_input, dict):
            return ""
        # Order matters: try the most-informative keys first, fall
        # back to whatever's there.
        priority_keys = (
            "file_path", "path", "pattern", "command", "query",
            "url", "old_string", "new_string", "prompt",
        )
        for k in priority_keys:
            v = tool_input.get(k)
            if isinstance(v, str) and v:
                snippet = v if len(v) <= 80 else v[:77] + "…"
                return f"{k}={snippet!r}"
        # Generic fallback: first key.
        for k, v in tool_input.items():
            if isinstance(v, (str, int, float, bool)):
                return f"{k}={v!r}"
        return ""

    def _pretty_clip(self, text: str, limit: int) -> str:
        """Truncate ``text`` to ``limit`` characters, appending ``…``
        when the original was longer. Pretty-transcript helper —
        intentionally keeps newlines (unlike ``_summarize_tool_input``
        which collapses to single-line key=value labels). Used for
        assistant text blocks where the CLI-terminal-style rendering
        wants the multi-line shape preserved."""
        if len(text) <= limit:
            return text
        return text[:limit - 1] + "…"

    def _pretty_format_tool_input(self, tool_input: dict) -> str:
        """Build a multi-key ``key: "v1", key: "v2"`` label for the
        Pretty transcript. More verbose than ``_summarize_tool_input``
        (which picks one key for the compact Activity line) — shows
        up to three priority keys so a Bash call surfaces both
        ``command`` and ``description``, a Read call surfaces both
        ``file_path`` and ``offset``, and so on.

        Returns the inside of the parens; the caller wraps as
        ``ToolName(<this>)``."""
        if not isinstance(tool_input, dict):
            return ""
        priority_keys = (
            "file_path", "path", "pattern", "command", "query",
            "url", "old_string", "new_string", "prompt",
            "description", "offset", "limit",
        )
        bits: list[str] = []
        for k in priority_keys:
            v = tool_input.get(k)
            if isinstance(v, str) and v:
                snippet = v if len(v) <= 120 else v[:117] + "…"
                bits.append(f'{k}: "{snippet}"')
            elif isinstance(v, bool):
                # ``bool`` first — bool IS-A int in Python, so the
                # numeric branch below would otherwise catch it and
                # render ``True``/``False`` capitalized. CLI style is
                # lowercase.
                bits.append(f"{k}: {str(v).lower()}")
            elif isinstance(v, (int, float)):
                bits.append(f"{k}: {v}")
            if len(bits) >= 3:
                break
        if not bits:
            # Generic fallback — same shape as _summarize_tool_input
            # but written in the Pretty ``key: value`` style.
            for k, v in tool_input.items():
                if isinstance(v, (str, int, float, bool)):
                    bits.append(f"{k}: {v!r}")
                    if len(bits) >= 3:
                        break
        return ", ".join(bits)

    def _pretty_format_tool_result_content(self, content) -> str:
        """Extract a one-line ~200-char excerpt of a tool_result's
        ``content`` field for the Pretty transcript. Returns ``""``
        when no usable text is present.

        ``content`` can be either a string (most tools) or a list of
        ``{type, text}`` blocks (some tools — e.g. Read returns a
        sequence). Mirrors the size-summing logic in
        ``_handle_stream_user`` but extracts the *text* rather than
        the length."""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for b in content:
                if isinstance(b, dict):
                    t = b.get("text", "")
                    if isinstance(t, str):
                        parts.append(t)
            text = "\n".join(parts)
        else:
            return ""
        text = text.strip()
        if not text:
            return ""
        first_line = text.splitlines()[0]
        truncated = len(first_line) > 200 or "\n" in text
        excerpt = first_line[:200]
        if truncated:
            excerpt += "…"
        return excerpt

    def _append_activity_html(
        self, ts: str, glyph: str, body_html: str,
    ) -> None:
        """Append one event line to the Activity tab as a new
        block (paragraph) in the underlying QTextDocument.

        Each event must be its own block — Qt's HTML-to-document
        converter is loose with `<div>` elements when called from a
        cursor that's already mid-document, frequently collapsing
        successive `<div>` inserts into inline content under the
        previous paragraph. The visible symptom is the whole
        activity log rendering as one wall of text. Explicit
        ``cursor.insertBlock()`` is the load-bearing piece — it's
        a primitive document operation, not HTML parsing, so it
        always inserts the paragraph boundary we asked for.

        Auto-scrolls to the bottom so the user always sees the
        latest event; the normal scrollbar still works for
        scrolling back to inspect earlier events."""
        cursor = self.activity_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # Skip the leading blank line on the very first append. An
        # empty QTextDocument has exactly one empty block (Qt's
        # default), so we fill that block on the first call and
        # only create new blocks from the second event onward.
        if not self.activity_view.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(
            f"<span style='color:#888;font-family:Consolas,monospace;"
            f"font-size:10pt;'>[{ts}]</span> "
            f"<span style='font-size:11pt;'>{glyph}</span> "
            f"<span>{body_html}</span>")
        self.activity_view.setTextCursor(cursor)
        self.activity_view.ensureCursorVisible()
        self._update_save_btn_state()

    def _fmt_ts(self) -> str:
        """Run-relative HH:MM:SS.mmm timestamp for Activity entries.

        Anchored at ``_run_started_at`` (set in ``_on_run``) so the
        first event is around ``00:00:00.xxx`` — easier to reason
        about than wall-clock time when comparing two runs of the
        same prompt."""
        if self._run_started_at is None:
            return "00:00:00.000"
        elapsed = time.monotonic() - self._run_started_at
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = elapsed % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    def _set_status_step(self, text: str) -> None:
        """Set the status label to a per-step summary, overriding
        the elapsed-time text from ``_on_tick`` until the next tick
        fires. 500ms cadence means the user briefly sees the step
        name before the countdown resumes — long enough to register,
        not long enough to fight the tick display for dominance."""
        self.status_label.setText(text)

    def _raw_view_append(self, text: str) -> None:
        """Append ``text`` to the visible Raw Output widget at the
        end, scrolling the cursor into view.

        Uses the **fully-qualified** ``QTextCursor.MoveOperation.End``
        enum reference, not the legacy ``cursor.End`` instance
        shorthand. PySide6 6.5+ enforces strict enum scoping by
        default and the shorthand raises ``AttributeError`` — that
        was the root cause of every "hang" symptom in §7.34-§7.41
        (§7.42).

        Not called directly from event sites — they go through the
        three buffer helpers below (which decide whether to write to
        the widget based on the active view mode)."""
        cursor = self.raw_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.raw_view.setTextCursor(cursor)
        self.raw_view.ensureCursorVisible()

    def _append_raw_buf(self, text: str) -> None:
        """Append to the raw JSON buffer (and to the visible widget
        when Raw JSON is the active mode). Used only for the verbatim
        stream-json event lines — every other Raw Output addition
        (preface, stderr, verdict, [timeout extended]) goes through
        ``_append_both`` since those texts are already CLI-readable."""
        self._raw_buffer_text += text
        if self._raw_view_mode == "raw":
            self._raw_view_append(text)
        self._update_save_btn_state()

    def _append_pretty_buf(self, text: str) -> None:
        """Append to the pretty CLI-style buffer (and to the visible
        widget when Pretty is the active mode). Called by the
        ``_handle_stream_*`` family in parallel with the existing
        Activity rendering — same parsed event, different output
        sink, more verbose presentation."""
        self._pretty_buffer_text += text
        if self._raw_view_mode == "pretty":
            self._raw_view_append(text)
        self._update_save_btn_state()

    def _append_both(self, text: str) -> None:
        """Append the same ``text`` to both internal buffers and to
        the visible widget. Used for the diagnostic preface, stderr
        lines, verdict markers ([done in Ns] / [cancelled] / [timed
        out] / [error]), and the [timeout extended] annotation.
        These lines are already in a human-readable shape, so
        rebuilding a "pretty" equivalent would only invent
        differences — show them verbatim in both views."""
        self._raw_buffer_text += text
        self._pretty_buffer_text += text
        self._raw_view_append(text)
        self._update_save_btn_state()

    def _on_raw_view_mode_changed(self, idx: int) -> None:
        """Slot for the Pretty / Raw JSON combobox.

        Reads the new mode from the combobox userData, stashes it,
        then replaces the widget contents from the corresponding
        internal buffer. Scroll position is intentionally NOT
        preserved — the two transcripts have different line counts
        and pixel heights, so a numeric scrollbar position from one
        doesn't translate to the other. Snapping to the bottom keeps
        the user pinned to "what's happening now" during a live run,
        which is the dominant usage."""
        mode = self.raw_view_mode_combo.itemData(idx)
        if not isinstance(mode, str) or mode not in ("pretty", "raw"):
            return
        if mode == self._raw_view_mode:
            return
        self._raw_view_mode = mode
        buf = (self._pretty_buffer_text if mode == "pretty"
               else self._raw_buffer_text)
        self.raw_view.setPlainText(buf)
        sb = self.raw_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())
        # Refresh the corner buttons — the active mode just changed,
        # so the Save As… tooltip ("Save the Pretty transcript…" vs
        # "Save the Raw JSON stream…") and the per-tab Clear button's
        # has-content check both need a recompute. Also the
        # ``raw_view`` now reflects whichever buffer just got swapped
        # in, so emptiness can change too (Pretty might have content,
        # Raw JSON might be empty, or vice versa).
        self._update_save_btn_state()
        _log(f"raw view mode → {mode}")

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
        """Clear Response, Activity, and Raw Output for a fresh run.
        Called from ``_on_run`` start and from the Clear button."""
        self.raw_view.clear()
        self.response_view.setMarkdown("")
        self.activity_view.clear()
        # Drop the markdown snapshot too — keeping it would let the
        # Save As… button claim there's a response to save when the
        # tab now visibly shows nothing. Stays in sync with the
        # rule: button enabled ⇔ tab has user-visible content.
        self._last_response_markdown = ""
        # Reset both Raw Output buffers so the next run starts with
        # an empty Pretty AND empty Raw JSON transcript — otherwise
        # flipping the toggle mid-next-run would reveal stale lines
        # from the previous run. Also clear the per-run
        # tool-call/result correlation field.
        self._raw_buffer_text = ""
        self._pretty_buffer_text = ""
        self._stream_last_tool_name = ""
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
        if idx == self._SUB_RESPONSE:
            has_content = bool(self._last_response_markdown.strip())
            ready_tip = "Save the rendered response as a .md file"
            empty_tip = "Run a prompt first — no response to save yet"
        elif idx == self._SUB_ACTIVITY:
            has_content = bool(
                self.activity_view.toPlainText().strip())
            ready_tip = (
                "Save the activity log (visible event narrative) "
                "as a .txt file")
            empty_tip = "Run a prompt first — no activity to save yet"
        else:  # Raw Output
            has_content = bool(self.raw_view.toPlainText().strip())
            # Mode-aware tooltip: the save path writes whatever's
            # currently visible (``raw_view.toPlainText()``), so name
            # the view-mode in the tooltip so the user knows which
            # transcript they're about to write to disk.
            mode_label = ("Pretty transcript"
                          if self._raw_view_mode == "pretty"
                          else "Raw JSON stream")
            ready_tip = (
                f"Save the {mode_label} (current view) as a .txt file")
            empty_tip = "Run a prompt first — no raw output to save yet"
        self._save_as_btn.setEnabled(has_content)
        self._save_as_btn.setToolTip(ready_tip if has_content else empty_tip)
        # Clear button has the same enablement contract as Save As…
        # (tab has content AND we aren't running), so propagate the
        # refresh from here rather than duplicating seven call sites.
        # Keeps Clear in lockstep with Save As… without parallel
        # bookkeeping.
        self._update_clear_tab_btn_state()

    def _update_clear_tab_btn_state(self) -> None:
        """Refresh the corner Clear button to reflect the visible
        inner tab.

        Disabled when the tab is empty (nothing to clear) and during
        a Run (wiping a tab the worker is still writing to would be
        immediately undone by the next stream event and would
        confuse the user about what's happening). Tooltip names the
        target tab so the user knows what's about to disappear.

        Don't call this directly from content-mutation sites — call
        ``_update_save_btn_state`` instead, which forwards here. The
        forward keeps both corner buttons (Save As… / Clear) in
        lockstep without seven duplicated update sites."""
        if self._is_running():
            self._clear_tab_btn.setEnabled(False)
            self._clear_tab_btn.setToolTip(
                "Disabled during a run — wouldn't make sense to "
                "wipe a tab the worker is still writing to.")
            return
        idx = self._response_tabs.currentIndex()
        if idx == self._SUB_RESPONSE:
            has_content = bool(self._last_response_markdown.strip())
            tab_name = "Response"
        elif idx == self._SUB_ACTIVITY:
            has_content = bool(
                self.activity_view.toPlainText().strip())
            tab_name = "Activity"
        else:  # Raw Output
            has_content = bool(self.raw_view.toPlainText().strip())
            tab_name = "Raw Output"
        self._clear_tab_btn.setEnabled(has_content)
        if has_content:
            self._clear_tab_btn.setToolTip(
                f"Clear the {tab_name} tab "
                "(other tabs and the prompt are untouched)")
        else:
            self._clear_tab_btn.setToolTip(
                f"Nothing to clear in the {tab_name} tab")

    def _on_clear_tab_clicked(self) -> None:
        """Corner button slot — wipes the currently-visible inner
        tab only. No confirmation prompt: the gesture is per-tab,
        the other two tabs retain their content, and a Run can
        always be re-executed if the user wants to regenerate.

        Distinct from ``self.clear_btn`` (next to Run/Cancel) which
        is the wholesale 'start over' gesture — prompt + all three
        panes + session id. This one is surgical; that one is total.

        Defensive ``_is_running`` guard mirrors ``_on_open_file_clicked``
        — the button is disabled mid-Run via
        ``_update_clear_tab_btn_state``, but a future keyboard
        shortcut could bypass the button entirely."""
        if self._is_running():
            return
        idx = self._response_tabs.currentIndex()
        if idx == self._SUB_RESPONSE:
            self.response_view.setMarkdown("")
            # Snapshot reset matters here too: otherwise Save As…
            # would still think there's content to save, mirroring
            # the same trap _clear_run_views guards against.
            self._last_response_markdown = ""
            kind = "Response"
        elif idx == self._SUB_ACTIVITY:
            self.activity_view.clear()
            kind = "Activity"
        else:
            self.raw_view.clear()
            # Same two-buffer reset as ``_clear_run_views`` — a
            # per-tab clear of Raw Output must wipe both modes,
            # otherwise toggling the combobox after clearing would
            # restore content from the other buffer and the visible
            # "clear" gesture would silently undo itself.
            self._raw_buffer_text = ""
            self._pretty_buffer_text = ""
            kind = "Raw Output"
        # _update_save_btn_state forwards to _update_clear_tab_btn_state,
        # so this one call refreshes both corner buttons against the
        # now-empty target tab.
        self._update_save_btn_state()
        self.status_label.setText(f"{kind} tab cleared")
        _log(f"per-tab clear: {kind}")

    def _on_save_as_clicked(self) -> None:
        """Corner button slot — dispatches by the visible inner tab.
        Disabled-state is also enforced in ``_update_save_btn_state``,
        but the dispatch still re-checks emptiness defensively in
        case a future entry point bypasses the helper."""
        idx = self._response_tabs.currentIndex()
        if idx == self._SUB_RESPONSE:
            self._save_response_as()
        elif idx == self._SUB_ACTIVITY:
            self._save_activity_as()
        else:
            self._save_raw_output_as()

    def _save_activity_as(self) -> None:
        """Write the Activity tab's visible event narrative to a
        ``.txt`` file. Saves the plain-text projection (timestamps +
        glyph + event summary), not the underlying HTML — the user
        wants the chronological log, and a .txt file pastes cleanly
        into bug reports or chat threads. The literal JSON event
        stream lives in Raw Output for users who need that level
        of detail."""
        text = self.activity_view.toPlainText()
        if not text.strip():
            return
        default = self._default_save_filename("activity", "txt")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Activity log as text",
            default,
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        self._write_text_file(Path(path), text, kind_label="activity")

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

    # ---- Open File… --------------------------------------------------------
    def _update_open_btn_tooltip(self) -> None:
        """Refresh the corner Open File… button's tooltip to describe
        which file kind the currently-visible inner tab will accept.
        Open File… is *not* gated by content (you can always load a
        file regardless of whether the tab is empty), only by the
        Run lifecycle — so this helper only tunes the tooltip and
        does not touch ``setEnabled``."""
        idx = self._response_tabs.currentIndex()
        if idx == self._SUB_RESPONSE:
            tip = "Open a Markdown (.md) file into the Response tab"
        elif idx == self._SUB_ACTIVITY:
            tip = "Open a text (.txt) file into the Activity tab"
        else:  # Raw Output
            tip = "Open a text (.txt) file into the Raw Output tab"
        self._open_file_btn.setToolTip(tip)

    def _on_open_file_clicked(self) -> None:
        """Corner button slot — dispatches by the visible inner tab.
        Mirrors ``_on_save_as_clicked``'s shape: one entry point,
        three per-tab loaders. Run-lifecycle gating (disable mid-
        Run) is enforced by toggling ``_open_file_btn.setEnabled``
        in ``_on_run`` / ``_finish_run``; this slot still runs only
        when the button is enabled."""
        idx = self._response_tabs.currentIndex()
        if idx == self._SUB_RESPONSE:
            self._open_response_file()
        elif idx == self._SUB_ACTIVITY:
            self._open_activity_file()
        else:
            self._open_raw_output_file()

    def _open_response_file(self) -> None:
        """Load a ``.md`` file into the Response tab. Routes through
        ``_set_response_markdown`` so the loaded text is also
        snapshot into ``_last_response_markdown`` — that means the
        Save As… button picks it up and, if the user re-saves, the
        round-trip is the original source, not Qt's HTML
        re-serialization."""
        path = self._prompt_open_path(
            title="Open Markdown into Response",
            file_filter="Markdown files (*.md);;All files (*)",
        )
        if path is None:
            return
        text = self._read_text_file(path, kind_label="markdown")
        if text is None:
            return
        self._set_response_markdown(text)
        self.status_label.setText(f"Loaded response ← {path.name}")
        _log(f"loaded response markdown: {path}")

    def _open_activity_file(self) -> None:
        """Load a ``.txt`` file into the Activity tab. The Activity
        view is a ``QTextBrowser`` normally populated with HTML
        from ``_append_activity_html``; loading replaces that with
        the plain text of the file via ``setPlainText`` — the user
        explicitly asked to view this file, so the live-event
        formatting is the wrong content shape here."""
        path = self._prompt_open_path(
            title="Open Text into Activity",
            file_filter="Text files (*.txt);;All files (*)",
        )
        if path is None:
            return
        text = self._read_text_file(path, kind_label="text")
        if text is None:
            return
        self.activity_view.setPlainText(text)
        # Loaded content is now saveable; refresh Save As… state so
        # the button reflects the new tab content. Same rule as the
        # _append_* callers.
        self._update_save_btn_state()
        # Snap to top so the user starts reading at the beginning,
        # not where Qt left the cursor after the bulk set.
        sb = self.activity_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(0)
        self.status_label.setText(f"Loaded activity ← {path.name}")
        _log(f"loaded activity text: {path}")

    def _open_raw_output_file(self) -> None:
        """Load a ``.txt`` file into the Raw Output tab. Same shape
        as ``_open_activity_file`` but writes to the
        ``QPlainTextEdit`` raw view instead."""
        path = self._prompt_open_path(
            title="Open Text into Raw Output",
            file_filter="Text files (*.txt);;All files (*)",
        )
        if path is None:
            return
        text = self._read_text_file(path, kind_label="text")
        if text is None:
            return
        self.raw_view.setPlainText(text)
        # The loaded text belongs to the active view only — we have
        # no way to know whether the file on disk was a Pretty
        # transcript or a Raw JSON dump (both save as ``.txt``).
        # Park the content in the active buffer, blank the other,
        # and let the toggle reveal an empty alternate view rather
        # than a stale buffer from the previous run.
        if self._raw_view_mode == "pretty":
            self._pretty_buffer_text = text
            self._raw_buffer_text = ""
        else:
            self._raw_buffer_text = text
            self._pretty_buffer_text = ""
        self._update_save_btn_state()
        sb = self.raw_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(0)
        self.status_label.setText(f"Loaded raw output ← {path.name}")
        _log(f"loaded raw output text: {path}")

    def _prompt_open_path(self, *, title: str, file_filter: str) -> Path | None:
        """Show the native file-open dialog rooted at the per-window
        working directory. Returns ``None`` if the user cancelled —
        no error, no toast, just exit the open flow."""
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            title,
            str(self._cwd),
            file_filter,
        )
        if not chosen:
            return None
        return Path(chosen)

    def _read_text_file(self, path: Path, *, kind_label: str) -> str | None:
        """Read ``path`` as UTF-8 text. Returns the string on success,
        ``None`` on failure (after surfacing the failure via
        ``QMessageBox.warning`` — mirrors ``_write_text_file``'s
        defense-at-the-boundary discipline so a permission or
        encoding error doesn't disappear into the log).

        UTF-8 is the only encoding tried — the save path emits UTF-8
        unconditionally (``_write_text_file``), so files this dialog
        wrote always round-trip cleanly. Foreign-encoded files
        produce a clear error rather than silently mojibake."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            _log(f"open failed: {path}: {e}")
            QMessageBox.warning(
                self, "Open failed",
                f"Could not open {kind_label} file:\n{path}\n\n{e}")
            return None
        _log(f"opened {kind_label} ({len(text)} chars): {path}")
        return text

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

    def _extend_timeout(self, delta_s: int) -> None:
        """Add ``delta_s`` seconds to the *current* run's timeout
        (§7.59).

        Increments ``_current_timeout_ms`` so the countdown display
        (driven by ``_on_tick``) and the eventual TIMED OUT verdict
        text both see the new total. Then restarts the backing
        ``_timeout_timer`` with the recomputed remaining slice —
        ``QTimer.start(msec)`` resets the single-shot from zero, so
        the extension must be ``new_total - elapsed``, *not*
        ``delta_s`` (a common off-by-elapsed bug if you forget Qt's
        timers don't accept "add to existing deadline").

        Per-run only — never touches ``QSettings``. The persisted
        Help → Settings… default is unchanged for the next Run and
        the next dialog open."""
        if not self._is_running() or self._run_started_at is None:
            return
        self._current_timeout_ms += delta_s * 1000
        elapsed_ms = int((time.monotonic() - self._run_started_at)
                         * 1000)
        # Guard against pathological clock skew or a delta that
        # somehow puts the new deadline behind ``now`` — give the
        # process at least one more second to drain. The +30s / +60s
        # / +5m presets can't produce this on their own (they only
        # extend), but the floor is cheap insurance and keeps the
        # method safe to call from a future "shrink" button.
        remaining_ms = max(1000,
                           self._current_timeout_ms - elapsed_ms)
        self._timeout_timer.start(remaining_ms)
        new_total_s = self._current_timeout_ms // 1000
        self._append_both(
            f"[timeout extended by {delta_s}s — "
            f"new total {new_total_s}s "
            f"(approx {remaining_ms // 1000}s remaining)]\n")
        _log(f"[gui] timeout extended by {delta_s}s; "
             f"new total {new_total_s}s, "
             f"remaining {remaining_ms}ms")

    def _on_clear(self) -> None:
        """Reset both panes back to the initial state, and explicitly
        forget any captured Claude conversation session (§7.46). Clear
        is the user's "start over" gesture — it should reset *all*
        run-derived state so the next Run is as if the dialog had just
        opened. With continue-mode now unconditional, Clear is the
        ONLY way to drop the captured session id mid-dialog; without
        it, every subsequent Run keeps resuming from the same thread.

        Doesn't touch in-flight run state — the GUI thread isn't
        running anything synchronously here (the worker is on its own
        thread), but the button is enabled regardless and Clear during
        a run is harmless: it wipes panes but doesn't cancel."""
        self.prompt_edit.clear()
        self._clear_run_views()
        # In-memory only — there is no persisted state to delete
        # (§7.68). Dropping the cwd alongside keeps the action-
        # boundary check honest: an empty (None, None) pair always
        # produces a fresh-start Run.
        self._session_id = None
        self._session_cwd = None
        self.status_label.setText("Idle")

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
        self._open_file_btn.setEnabled(True)
        self.run_busy.hide()
        for btn in self._extend_buttons:
            btn.hide()
        # Snap back to the Response tab now that the run is over —
        # mirror of the switch-to-Activity at the start of ``_on_run``.
        # All three reach-the-finish-line callers
        # (``_on_worker_result``, ``_on_worker_failed``, and the
        # outer ``_on_run`` try/except) route through here, so this
        # is the single place to enforce the post-run focus. Every
        # real-finish path populates Response via
        # ``_set_response_markdown`` before calling us — even
        # failure / cancel / timeout — so the snap-back lands on
        # content the user wants to read (the answer, or a
        # "see Raw Output" pointer).
        self._response_tabs.setCurrentIndex(self._SUB_RESPONSE)
        # Re-evaluate corner button enablement now that the worker
        # thread is gone (``_is_running()`` is False above this
        # point). _set_response_markdown was called BEFORE us in
        # every real-finish path, so Save As… is already in the
        # right state — but Clear was forced off mid-Run and its
        # gate (``_is_running()``) only flipped just above. One
        # call refreshes both via the forward in
        # _update_save_btn_state.
        self._update_save_btn_state()

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
