"""Entry point for the Claude Skills Manager GUI."""
from __future__ import annotations

import sys

from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from claude_skills_manager.logging_setup import configure_logging, log_qt_message
from claude_skills_manager.ui.app_icon import app_icon
from claude_skills_manager.ui.main_window import MainWindow
from claude_skills_manager.ui.splash import SplashWindow

# AppUserModelID — a process-level identity string used by the Windows
# taskbar to decide which icon to display for a running app. Without an
# explicit value the OS inherits python.exe's identity, so the taskbar
# shows the Python interpreter's icon regardless of what
# QApplication.setWindowIcon() says. Set ours before any window is
# shown so the taskbar picks up our QIcon. Format convention is
# "Company.Product[.SubProduct[.Version]]"; matches the QSettings
# organization/app pair so all per-user state for this app keys off the
# same identity. (Windows-only; harmlessly skipped on macOS/Linux.)
_WINDOWS_APP_ID = "ClaudeSkillsManager.ClaudeSkillsManager"


_QT_SEVERITY = {
    QtMsgType.QtDebugMsg:    "debug",
    QtMsgType.QtInfoMsg:     "info",
    QtMsgType.QtWarningMsg:  "warning",
    QtMsgType.QtCriticalMsg: "critical",
    QtMsgType.QtFatalMsg:    "fatal",
}


def _qt_message_handler(mode, context, message: str) -> None:
    """Custom Qt logging handler that filters one specific cosmetic
    warning while routing everything else through Python ``logging`` →
    the configured log file.

    The filtered pattern is Qt's Windows QPA screen backend logging
    SetupAPI failures (``CR_NO_SUCH_VALUE`` / ``0xe0000225``) when it
    queries monitor info during routine screen-update polling — fires
    on display sleep/wake, RDP sessions, USB-C dock unplugs, dynamic
    refresh rate transitions, and multi-monitor setups where one
    display's EDID is incomplete. Qt falls back to defaults internally
    and the app continues to function; the message is purely a log
    artifact. See §7.31 for the full diagnosis."""
    if mode == QtMsgType.QtWarningMsg and \
            "Unable to open monitor interface" in message:
        return
    try:
        category = (context.category or "").strip()
    except (AttributeError, TypeError):
        category = ""
    severity = _QT_SEVERITY.get(mode, "warning")
    log_qt_message(category, message, severity)


def _set_windows_taskbar_identity() -> None:
    """Register a per-app AppUserModelID so the Windows taskbar shows
    the custom icon set via ``QApplication.setWindowIcon`` instead of
    inheriting python.exe's identity. No-op outside Windows; failures
    on Windows fall back silently (taskbar shows the python.exe icon
    but in-app surfaces — title bar, toolbar, Alt+Tab — still get the
    custom logo because those paths don't depend on the AppUserModelID).
    Must run BEFORE the first QWindow is shown."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            _WINDOWS_APP_ID)
    except (AttributeError, OSError):
        # AttributeError: older Windows without the shell32 export.
        # OSError: shell32 unavailable in some sandboxed environments.
        pass


def main() -> int:
    # File logging FIRST — every diagnostic from here on (including the
    # Qt warnings routed through `_qt_message_handler`) lands in the
    # same per-launch log file. Truncation means a "launch, repro, send
    # the log" flow gives a clean trace per bug report.
    configure_logging()
    # Install the Qt logging filter before anything else so even very
    # early Qt warnings (e.g., during QApplication construction) are
    # routed through our handler.
    qInstallMessageHandler(_qt_message_handler)
    # Identity registration must happen before QApplication so the
    # taskbar binds to our AppUserModelID, not python.exe's, when the
    # main window is shown.
    _set_windows_taskbar_identity()

    app = QApplication(sys.argv)
    app.setApplicationName("Claude Skills Manager")
    app.setOrganizationName("ClaudeSkillsManager")
    # Setting on the QApplication propagates to any window that doesn't
    # set its own icon — covers title bar, taskbar, and Alt+Tab in one
    # call. Must come AFTER QApplication construction because app_icon()
    # constructs QPixmaps internally.
    app.setWindowIcon(app_icon())

    # Splash window covers the gap between launch and the first scan
    # completing. The flow is intentionally sync:
    #
    #   1. Show splash + force first paint.
    #   2. Construct MainWindow with ``defer_initial_scan=True`` so its
    #      built-in QTimer.singleShot(0, refresh) no-ops — without this,
    #      the scan would race with the splash and could fire either
    #      before or after we close it, depending on event-loop timing.
    #   3. Run the initial scan synchronously via ``window.refresh()``
    #      while the splash is the visible surface. ``set_status`` pumps
    #      the event loop so the splash repaints between phases.
    #   4. Close splash, then show window. Order matters: if we showed
    #      the window first, the user would briefly see an empty main
    #      window above the still-up splash before the splash closed,
    #      which looks like a launch glitch.
    #
    # Refresh / F5 / Choose-root / right-click delete paths do NOT
    # re-trigger the splash — those still flow through MainWindow's
    # status-bar busy indicator. This is deliberate: stealing the user
    # back to a launch-screen on every refresh would be jarring.
    splash = SplashWindow()
    splash.show()
    app.processEvents()

    splash.set_status("Loading interface…")
    window = MainWindow(defer_initial_scan=True)

    splash.set_status("Scanning skills…")
    # Pass app.processEvents as the scanner's progress pump (§7.62)
    # so the splash's indeterminate marquee actually animates during
    # the scan. Without this, the sync scan blocks the main thread
    # and Qt's animation tick never fires — the marquee appears
    # frozen. F5 / Refresh / Choose-root paths intentionally don't
    # pump (they pass on_progress=None), since pumping while
    # MainWindow is visible could process queued user input
    # mid-scan; the splash startup is the only window where the
    # pump is unambiguously safe (no interactive widgets are on
    # screen besides the splash itself).
    window.refresh(on_progress=app.processEvents)

    splash.close()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
