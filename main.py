"""Entry point for the Claude Skills Manager GUI."""
from __future__ import annotations

import sys

from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from claude_skills_manager.ui.app_icon import app_icon
from claude_skills_manager.ui.main_window import MainWindow

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


def _qt_message_handler(mode, context, message: str) -> None:
    """Custom Qt logging handler that filters one specific cosmetic
    warning while passing everything else through to stderr.

    The filtered pattern is Qt's Windows QPA screen backend logging
    SetupAPI failures (``CR_NO_SUCH_VALUE`` / ``0xe0000225``) when it
    queries monitor info during routine screen-update polling — fires
    on display sleep/wake, RDP sessions, USB-C dock unplugs, dynamic
    refresh rate transitions, and multi-monitor setups where one
    display's EDID is incomplete. Qt falls back to defaults internally
    and the app continues to function; the message is purely a log
    artifact. See §7.31 for the full diagnosis.

    Everything else (other warnings, info, debug, critical, fatal) is
    forwarded to stderr with the category prefix so real Qt-side
    issues still surface. The category prefix mimics Qt's default
    formatter so the console output stays consistent for unfiltered
    messages."""
    if mode == QtMsgType.QtWarningMsg and \
            "Unable to open monitor interface" in message:
        return
    try:
        category = (context.category or "").strip()
    except (AttributeError, TypeError):
        category = ""
    prefix = f"{category}: " if category else ""
    sys.stderr.write(f"{prefix}{message}\n")


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

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
