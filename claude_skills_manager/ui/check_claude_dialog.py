"""Claude CLI health check dialog (§7.36 + §7.37).

Two-step diagnostic for the user's ``claude`` install. Each step
isolates one variable so a failure points at exactly one layer:

* **Step 1 — `claude --version`** runs the binary with no auth, no
  network, no model invocation. If this fails the CLI itself is
  broken (not on PATH, corrupt install, etc.). Auto-runs when the
  dialog first appears.
* **Step 2 — `claude -p "<trivial prompt>"`** does a full round-trip
  through auth, network, and the model. If Step 1 passed and Step 2
  hangs, the issue is in the CLI's runtime layers (typically first-
  time auth needing interactive setup, or rate-limit / network).
  Run manually — it can take 30s+ on slow networks, and the user
  may not want to spend the time on every dialog open.

A "Copy Command" button puts the relevant invocation on the
clipboard so the user can paste it into a terminal and see what
happens *outside* our QProcess wrapper. If the terminal hangs the
same way, the wrapper isn't the issue; if the terminal works,
there's a QProcess-side bug worth filing.

The dialog wraps both runs in ``try/except``, surfacing any failure
as visible text in the output pane. PySide6 silently swallows
exceptions raised from queued slot calls (``QTimer.singleShot``,
signal callbacks), so without explicit handling a partial failure
inside ``_start_run`` would leave the UI in a half-toggled state
with no message — exactly the "Idle + button disabled + no output"
symptom from §7.37."""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from .. import app_settings
from ..skill_introspect import (
    CLAUDE_EXECUTABLE, build_claude_command, claude_env_overrides,
    claude_path_diagnostic, find_claude_executable,
)
from ._styles import BUTTON_STYLE


# Prompt for Step 2. Two characters is short enough that any
# meaningful latency is clearly setup-related, not the model
# thinking; specific enough that any response at all proves the
# end-to-end pipeline (auth + network + model) is functional.
_PROMPT_TEXT = "Reply with exactly the two characters: OK"

# Step 1 timeout is fixed — ``claude --version`` should complete in
# <1s on any sane install; 10s allows for slow filesystems or AV
# scanning on Windows. A user-configurable Step 1 wouldn't earn its
# complexity.
_VERSION_TIMEOUT_MS = 10_000


def _prompt_timeout_ms() -> int:
    """Step 2 (``claude -p``) timeout in ms — read from the same
    user setting that drives per-skill test runs (Help → Settings…).
    Returned per-call so a setting change applies to the next Run
    click without reopening the dialog."""
    return app_settings.get_test_timeout_ms()


class CheckClaudeDialog(QDialog):
    """Modeless dialog running staged ``claude`` CLI health checks.

    Singleton at the MainWindow level — one instance at a time;
    same WA_DeleteOnClose + ``closed`` signal pattern as
    ``TestSkillDialog``."""

    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlags(Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle("Claude CLI Health Check")
        self.resize(760, 620)
        self.setMinimumSize(620, 480)

        # Current run state. ``_kind`` is "version" or "prompt" so
        # the finished/error/timeout handlers know which step's
        # status to update.
        self._process: QProcess | None = None
        self._kind: str | None = None
        self._run_started_at: float | None = None
        self._received_bytes: int = 0
        self._was_cancelled = False
        self._timed_out = False
        # Guard so the auto-run only fires on the first showEvent —
        # subsequent shows (raise from minimized, re-focus, etc.)
        # should NOT re-trigger the version check.
        self._auto_run_done = False

        # Tick timer drives the elapsed-time + countdown display
        # every 500ms (§7.35). Reused across both steps.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(500)
        self._tick_timer.timeout.connect(self._on_tick)

        # Single-shot timeout timer. ``setSingleShot`` so we don't
        # have to remember to stop it after firing; started with
        # the per-step timeout inside ``_start_run``.
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)

        self._build_ui()

    # ---------------------------------------------------------- show hook
    def showEvent(self, event) -> None:  # noqa: N802 — Qt naming
        """Auto-run Step 1 the first time the dialog is shown.

        ``showEvent`` is preferred over ``QTimer.singleShot(0, …)``
        from ``__init__`` because:

        * It fires deterministically after Qt has rendered the
          window — widgets are guaranteed ready to receive updates.
        * If the dialog construction raised, ``showEvent`` would
          never fire (correct fail-safe); a queued ``singleShot``
          slot might run against a half-built dialog.
        * It's robust to Qt versions where queued zero-delay
          timers don't fire reliably (the §7.37 symptom).
        """
        super().showEvent(event)
        if not self._auto_run_done:
            self._auto_run_done = True
            # Defer to next tick so the window paint completes
            # before we start mutating widget state — gives the
            # user a clean "dialog appeared, now Step 1 is
            # running" transition rather than a frozen first
            # frame.
            QTimer.singleShot(0, self._run_version)

    # ----------------------------------------------------------- UI build
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        intro = QLabel(
            "<b>Claude CLI Health Check</b><br>"
            "Two-step diagnostic for your <code>claude</code> install. "
            "Step 1 verifies the binary itself; Step 2 exercises a full "
            "auth + network + model round-trip. If Step 1 fails the CLI "
            "is broken; if Step 1 passes but Step 2 hangs, the issue is "
            "in auth/network."
        )
        intro.setTextFormat(Qt.RichText)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Resolved-binary banner — show the user immediately whether
        # we located ``claude`` on disk and where (§7.38). Painted in
        # green when found, amber when falling back to the bare name
        # (which usually means QProcess will fail to launch).
        layout.addWidget(self._build_binary_banner())

        # ---- Step 1: --version ----
        layout.addWidget(self._build_step_box(
            step="version",
            title="Step 1: <code>claude --version</code>",
            hint=("Runs the CLI binary with no auth, no network, "
                  "no model invocation. Should return in &lt;1s. "
                  "Runs automatically when this window opens."),
        ))

        # ---- Step 2: prompt round-trip ----
        layout.addWidget(self._build_step_box(
            step="prompt",
            title=f"Step 2: <code>claude -p \"{_PROMPT_TEXT}\"</code>",
            hint=("Full end-to-end round-trip — auth, network, and "
                  "the model. Typically 5-30s. Click <b>Run</b> when "
                  "ready; skip if Step 1 failed."),
        ))

        # ---- Output (shared, shows most-recent run) ----
        out_label = QLabel("<b>Output (most recent run):</b>")
        out_label.setTextFormat(Qt.RichText)
        layout.addWidget(out_label)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(10)
        self.output.setFont(font)
        self.output.setPlaceholderText(
            "(Step 1 will run automatically when this window appears. "
            "Output of the most recent run shows here.)")
        layout.addWidget(self.output, 1)

        # ---- Bottom buttons ----
        bar = QHBoxLayout()
        copy_v = QPushButton("Copy `--version` Command")
        copy_v.setStyleSheet(BUTTON_STYLE)
        copy_v.setToolTip(
            "Copy `claude --version` to your clipboard for manual "
            "terminal testing.")
        copy_v.clicked.connect(
            lambda: self._copy_command(f"{CLAUDE_EXECUTABLE} --version"))
        copy_p = QPushButton("Copy Prompt Command")
        copy_p.setStyleSheet(BUTTON_STYLE)
        copy_p.setToolTip(
            "Copy the Step 2 command to your clipboard — paste it in "
            "a terminal to see what `claude` does outside this app.")
        copy_p.clicked.connect(
            lambda: self._copy_command(
                f'{CLAUDE_EXECUTABLE} -p "{_PROMPT_TEXT}"'))
        bar.addWidget(copy_v)
        bar.addWidget(copy_p)
        bar.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(BUTTON_STYLE)
        close_btn.setShortcut("Esc")
        close_btn.clicked.connect(self.close)
        bar.addWidget(close_btn)
        layout.addLayout(bar)

    def _build_binary_banner(self) -> QLabel:
        """Banner showing the resolved ``claude`` path. Updated by
        ``_refresh_binary_banner`` — called once at build time and
        also after each ``FailedToStart`` so a fix-and-retry workflow
        shows the new state without reopening the dialog."""
        self.binary_banner = QLabel()
        self.binary_banner.setTextFormat(Qt.RichText)
        self.binary_banner.setWordWrap(True)
        self.binary_banner.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.binary_banner.setStyleSheet(
            "padding:6px 10px; border-radius:4px;")
        self._refresh_binary_banner()
        return self.binary_banner

    def _refresh_binary_banner(self) -> None:
        """Re-resolve the ``claude`` path and update the banner.
        Green box if found, red box if not — colors chosen for
        Windows readability against the dialog's default light
        background."""
        resolved = find_claude_executable()
        if resolved:
            self.binary_banner.setStyleSheet(
                "padding:6px 10px; border-radius:4px; "
                "background:#e6f3e6; color:#1f4a1f; "
                "border:1px solid #b3d6b3;")
            self.binary_banner.setText(
                f"<b>Resolved <code>claude</code> at:</b> "
                f"<code>{resolved}</code>")
        else:
            self.binary_banner.setStyleSheet(
                "padding:6px 10px; border-radius:4px; "
                "background:#fceae9; color:#7a1f1f; "
                "border:1px solid #e6b3b0;")
            self.binary_banner.setText(
                f"<b>Could not locate <code>claude</code> on disk.</b> "
                f"Run Step 1 to see the full PATH diagnostic in the "
                f"output pane.")

    def _build_step_box(self, *, step: str, title: str, hint: str) -> QFrame:
        """Build one of the two step boxes. Returns the QFrame to be
        placed into the main layout. Stores button + status references
        on ``self`` under names derived from ``step`` so the run
        handlers can reach them without per-step branching."""
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(
            "QFrame { background:#fafafa; border:1px solid #d0d0d0; "
            "border-radius:4px; }")
        v = QVBoxLayout(frame)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        title_lbl = QLabel(title)
        title_lbl.setTextFormat(Qt.RichText)
        title_lbl.setStyleSheet("font-weight:bold; font-size:11pt;")
        v.addWidget(title_lbl)

        hint_lbl = QLabel(hint)
        hint_lbl.setTextFormat(Qt.RichText)
        hint_lbl.setWordWrap(True)
        hint_lbl.setStyleSheet("color:#666; font-size:9pt;")
        v.addWidget(hint_lbl)

        row = QHBoxLayout()
        status = QLabel("Not yet run")
        status.setStyleSheet("color:#666; padding:4px 0; font-weight:bold;")
        status.setWordWrap(True)
        row.addWidget(status, 1)

        run_btn = QPushButton("Run")
        run_btn.setStyleSheet(BUTTON_STYLE)
        if step == "version":
            run_btn.clicked.connect(self._run_version)
        else:
            run_btn.clicked.connect(self._run_prompt)
        row.addWidget(run_btn)
        v.addLayout(row)

        # Stash widget refs on self for the run handlers. Using
        # attribute names rather than a dict keeps the call sites
        # explicit (``self._version_status`` reads as a member
        # access, not a lookup).
        if step == "version":
            self._version_status = status
            self._version_btn = run_btn
        else:
            self._prompt_status = status
            self._prompt_btn = run_btn

        return frame

    # ------------------------------------------------------- run entrypoints
    def _run_version(self) -> None:
        # Resolve fresh on every run so an install-during-app-runtime
        # gets picked up next click (§7.38).
        exe = find_claude_executable() or CLAUDE_EXECUTABLE
        self._start_run(
            kind="version",
            argv=[exe, "--version"],
            timeout_ms=_VERSION_TIMEOUT_MS,
            label=f"Step 1: {exe} --version",
        )

    def _run_prompt(self) -> None:
        # Pull live values from Settings so a change there applies on
        # the next click. Same wiring as the per-skill test dialog.
        model = app_settings.get_model()
        api_key = app_settings.get_api_key()
        argv = build_claude_command(_PROMPT_TEXT, model=model)
        env_overrides = claude_env_overrides(api_key)
        # Build a label that mirrors the actual invocation for the
        # diagnostic preface — copy-pasteable into a terminal.
        label_parts = [f"Step 2: {argv[0]} --print \"{_PROMPT_TEXT}\""]
        if model:
            label_parts.append(f"--model {model}")
        if api_key:
            label_parts.append("(env: ANTHROPIC_API_KEY override active)")
        self._start_run(
            kind="prompt",
            argv=argv,
            timeout_ms=_prompt_timeout_ms(),
            label=" ".join(label_parts),
            env_overrides=env_overrides,
        )

    def _start_run(
        self, *, kind: str, argv: list[str], timeout_ms: int, label: str,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        """Single entry point that builds and starts a QProcess for
        either step. Wrapped in ``try/except`` so any failure inside
        QProcess construction / signal wiring / start() lands in the
        output pane as visible text rather than being silently
        swallowed by PySide6's queued-slot handling — that swallowing
        was the §7.37 'Idle + button disabled + no output' symptom."""
        if self._process is not None:
            return  # busy

        try:
            self._kind = kind
            self._was_cancelled = False
            self._timed_out = False
            self._received_bytes = 0
            self._run_started_at = time.monotonic()

            # Disable both step buttons — only one check runs at a
            # time, and the shared output pane would get confused
            # by interleaved chunks otherwise.
            self._version_btn.setEnabled(False)
            self._prompt_btn.setEnabled(False)

            # Update THIS step's status to "Running…" (with color
            # reset to neutral so a previous red FAILED doesn't
            # leak into the new run).
            status_widget = self._status_for(kind)
            status_widget.setStyleSheet(
                "color:#1f4e8a; padding:4px 0; font-weight:bold;")
            status_widget.setText("Starting…")

            # Clear the output pane and write the diagnostic preface.
            # Showing exactly what's about to run gives the user
            # something to read while waiting AND something to copy
            # if they need to manually reproduce.
            self.output.clear()
            cwd = str(Path.home())
            diag = [
                f"=== {label} ===",
                f"[cmd] {' '.join(argv)}",
                f"[cwd] {cwd}",
                f"[timeout] {timeout_ms // 1000}s",
                "",
            ]
            self._append_output("\n".join(diag) + "\n")

            # QProcess parented to self for auto-cleanup; signals
            # wired before start() so we don't miss an early
            # errorOccurred fire on FailedToStart.
            self._process = QProcess(self)
            self._process.setWorkingDirectory(cwd)
            self._process.setProcessChannelMode(QProcess.SeparateChannels)
            # Apply env overrides BEFORE start(): QProcess snapshots the
            # environment at start time, so post-start setProcessEnvironment
            # calls wouldn't take effect. systemEnvironment() seeds with
            # the parent env so we don't accidentally launch claude into
            # a stripped-down environment (no PATH, no APPDATA, etc.).
            if env_overrides:
                qenv = QProcessEnvironment.systemEnvironment()
                for key, value in env_overrides.items():
                    qenv.insert(key, value)
                self._process.setProcessEnvironment(qenv)
            self._process.readyReadStandardOutput.connect(self._on_stdout)
            self._process.readyReadStandardError.connect(self._on_stderr)
            self._process.started.connect(self._on_started)
            self._process.finished.connect(self._on_finished)
            self._process.errorOccurred.connect(self._on_error)
            self._process.start(argv[0], argv[1:])
            # Close our end of the child's stdin so `claude` knows no
            # piped input is coming and commits to processing the
            # argv prompt alone. Without this, ``claude -p`` may read
            # stdin in case the caller intends to extend the prompt
            # via pipe — and since QProcess connects stdin by
            # default, the child waits forever on a read that will
            # never come. See §7.39 for the symptom (Test Skill
            # hangs even on trivial prompts while Check Claude
            # happened to work by luck). Called AFTER start() so
            # the channel exists; safe to call even if start
            # ultimately fails (no-op on a closed QProcess).
            self._process.closeWriteChannel()

            self._tick_timer.start()
            self._timeout_timer.start(timeout_ms)

        except Exception as e:
            # Catch-all so the user sees what failed instead of a
            # silently half-toggled UI. ``traceback.format_exc()``
            # gives the line number — invaluable for diagnosing
            # something like a missing import or a typo'd attribute.
            tb = traceback.format_exc()
            self._append_output(
                f"\n[INTERNAL ERROR while starting {kind} check]\n"
                f"{tb}\n")
            self._status_for(kind).setStyleSheet(
                "color:#b8232f; padding:4px 0; font-weight:bold;")
            self._status_for(kind).setText(f"ERROR: {e}")
            self._teardown_process()

    # -------------------------------------------------------- QProcess slots
    def _on_started(self) -> None:
        """OS-confirmed launch. Flip 'Starting…' → 'Running…' so the
        user has positive evidence the binary started executing."""
        if self._kind is None:
            return
        self._update_running_status()

    def _on_tick(self) -> None:
        if self._process is None or self._run_started_at is None:
            return
        self._update_running_status()

    def _update_running_status(self) -> None:
        """Tick the elapsed-time + countdown on whichever step is
        currently running. Bimodal: 'no output yet' vs 'receiving'
        (same pattern as the per-skill test dialog §7.35)."""
        if self._run_started_at is None or self._kind is None:
            return
        elapsed = time.monotonic() - self._run_started_at
        timeout_s = (_VERSION_TIMEOUT_MS if self._kind == "version"
                     else _prompt_timeout_ms()) / 1000
        remaining = max(0.0, timeout_s - elapsed)
        status = self._status_for(self._kind)
        if self._received_bytes == 0:
            status.setText(
                f"Running… {elapsed:.1f}s · waiting for output "
                f"(timeout in {remaining:.0f}s)")
        else:
            status.setText(
                f"Receiving… {elapsed:.1f}s · "
                f"{self._received_bytes:,} bytes")

    def _on_stdout(self) -> None:
        if self._process is None:
            return
        try:
            data = self._process.readAllStandardOutput()
            text = bytes(data).decode("utf-8", errors="replace")
            self._received_bytes += len(text)
            self._append_output(text)
        except Exception:
            # Defensive — if decode or widget update fails for any
            # reason, log it and keep the run alive rather than
            # silently dropping output.
            self._append_output(
                f"\n[INTERNAL ERROR reading stdout]\n"
                f"{traceback.format_exc()}\n")

    def _on_stderr(self) -> None:
        if self._process is None:
            return
        try:
            data = self._process.readAllStandardError()
            text = bytes(data).decode("utf-8", errors="replace")
            if text.strip():
                self._received_bytes += len(text)
                self._append_output(f"[stderr] {text}")
        except Exception:
            self._append_output(
                f"\n[INTERNAL ERROR reading stderr]\n"
                f"{traceback.format_exc()}\n")

    def _append_output(self, text: str) -> None:
        """Append to the output pane and keep the view scrolled to
        the bottom.

        Uses the **fully-qualified** ``QTextCursor.MoveOperation.End``
        enum reference, not the legacy ``cursor.End`` instance
        shorthand. PySide6 6.5+ enforces strict enum scoping by
        default and the shorthand raises ``AttributeError`` — that
        was the root cause of every "hang" symptom in §7.34-§7.41.
        See §7.42 for the full diagnosis chain.

        The previous defensive ``try/except: pass`` wrapper around
        these calls is **removed**: it was hiding the AttributeError
        that we needed to see. A failed widget update should surface,
        not silently no-op."""
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        # Final drain — see §7.34 commentary.
        self._on_stdout()
        self._on_stderr()

        duration = 0.0
        if self._run_started_at is not None:
            duration = time.monotonic() - self._run_started_at
        kind = self._kind or "?"
        status = self._status_for(kind) if kind != "?" else None

        verdict, color, marker = self._classify_result(
            kind=kind,
            duration=duration,
            exit_code=exit_code,
            exit_status=exit_status,
        )
        if status is not None:
            status.setStyleSheet(
                f"color:{color}; padding:4px 0; font-weight:bold;")
            status.setText(verdict)
        self._append_output(f"\n{marker}\n")
        self._teardown_process()

    def _classify_result(
        self, *, kind: str, duration: float, exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> tuple[str, str, str]:
        """Decide the colored verdict + closing pane marker from the
        final state. Returns (verdict_text, css_color, marker_text).
        Factored out so the branching tree stays readable —
        ``_on_finished`` itself just delegates and updates widgets."""
        if self._timed_out:
            limit = (_VERSION_TIMEOUT_MS if kind == "version"
                     else _prompt_timeout_ms()) / 1000
            return (
                f"FAILED: timed out after {duration:.1f}s (limit {limit:.0f}s)",
                "#b8232f",
                f"[timed out after {duration:.1f}s]",
            )
        if self._was_cancelled:
            return (
                f"Cancelled after {duration:.1f}s",
                "#444",
                f"[cancelled by user after {duration:.1f}s]",
            )
        if exit_status == QProcess.CrashExit:
            return (
                f"FAILED: process crashed after {duration:.1f}s",
                "#b8232f",
                f"[process crashed after {duration:.1f}s]",
            )
        if exit_code != 0:
            return (
                f"FAILED: exit code {exit_code} after {duration:.1f}s",
                "#b8232f",
                f"[exited with code {exit_code} after {duration:.1f}s]",
            )
        if self._received_bytes == 0:
            return (
                f"DONE but with NO output (exit 0 in {duration:.1f}s) — "
                f"unusual; check `claude` config",
                "#b87a23",  # amber
                f"[done in {duration:.1f}s but no output]",
            )
        return (
            f"DONE in {duration:.1f}s · "
            f"{self._received_bytes:,} bytes received",
            "#1f7a37",  # green
            f"[done in {duration:.1f}s · "
            f"{self._received_bytes:,} bytes received]",
        )

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.FailedToStart:
            kind = self._kind or "?"
            # Full PATH diagnostic so the user can see exactly what
            # we tried and what's on PATH — way more actionable than
            # a generic "not found" message (§7.38).
            diagnostic = claude_path_diagnostic()
            self._append_output(
                f"\n[error] QProcess could not launch `claude`.\n"
                f"\n--- Path resolution diagnostic ---\n"
                f"{diagnostic}\n"
                f"--- End diagnostic ---\n")
            if kind != "?":
                self._status_for(kind).setStyleSheet(
                    "color:#b8232f; padding:4px 0; font-weight:bold;")
                self._status_for(kind).setText(
                    "FAILED: could not launch `claude` — "
                    "see PATH diagnostic in output pane")
            # Refresh the banner — if the user installs claude and
            # re-clicks Run, the next attempt will pick up the new
            # state, and the banner should reflect that on the
            # subsequent attempt.
            self._refresh_binary_banner()
            # FailedToStart doesn't trigger finished(); reset here.
            self._teardown_process()
        elif error == QProcess.Crashed:
            return  # finished() will handle
        else:
            self._append_output(
                f"\n[error] Unexpected QProcess error code {int(error)}\n")
            # Other errors don't reliably trigger finished(); reset.
            self._teardown_process()

    def _on_timeout(self) -> None:
        if self._process is None:
            return
        self._timed_out = True
        self._status_for(self._kind or "version").setText(
            "Timing out — killing process…")
        self._process.kill()

    # ------------------------------------------------------------- helpers
    def _status_for(self, kind: str) -> QLabel:
        return self._version_status if kind == "version" else self._prompt_status

    def _copy_command(self, command: str) -> None:
        QApplication.clipboard().setText(command)
        # Brief acknowledgment in the output pane — keeps the
        # interaction visible without grabbing a transient toast
        # widget, which we don't have.
        self._append_output(
            f"\n[clipboard] Copied: {command}\n"
            f"  → Paste this into a fresh PowerShell window to test "
            f"`claude` outside this app.\n")

    def _teardown_process(self) -> None:
        self._tick_timer.stop()
        self._timeout_timer.stop()
        if self._process is not None:
            try:
                self._process.readyReadStandardOutput.disconnect(self._on_stdout)
                self._process.readyReadStandardError.disconnect(self._on_stderr)
                self._process.started.disconnect(self._on_started)
                self._process.finished.disconnect(self._on_finished)
                self._process.errorOccurred.disconnect(self._on_error)
            except (RuntimeError, TypeError):
                pass
            self._process.deleteLater()
            self._process = None
        self._kind = None
        # Re-enable both step buttons regardless of which was
        # running — the next click of either should be allowed.
        self._version_btn.setEnabled(True)
        self._prompt_btn.setEnabled(True)
        # First-time button label "Run" → "Re-run" after the run
        # has happened, so the user can see they've already tried
        # this step.
        if self._version_btn.text() == "Run":
            self._version_btn.setText("Re-run")
        if self._prompt_btn.text() == "Run" and self._has_prompt_run:
            self._prompt_btn.setText("Re-run")

    @property
    def _has_prompt_run(self) -> bool:
        """Hint flag: True iff the prompt step's status has changed
        from its initial 'Not yet run'. Used to decide whether to
        relabel its button to 'Re-run'. Cheap text check rather than
        an extra state field — the text IS the state."""
        return self._prompt_status.text() != "Not yet run"

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt naming
        if self._process is not None:
            self._was_cancelled = True
            self._process.kill()
            self._process.waitForFinished(1500)
        self.closed.emit()
        super().closeEvent(event)
