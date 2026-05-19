"""Startup splash window shown while the initial skill scan runs.

Lifetime is bounded by app launch:

1. ``main.py`` constructs and ``show()``s a :class:`SplashWindow`.
2. It then constructs the main window with ``defer_initial_scan=True``
   (so MainWindow's usual ``QTimer.singleShot(0, refresh)`` no-ops).
3. ``main.py`` calls ``window.refresh()`` directly — synchronous, with
   the splash still painted as the visible surface.
4. ``splash.close()`` + ``window.show()``.

Splash is **app-start only**. Refresh / F5 / Choose-root / context-menu
deletes still go through the existing in-status-bar "busy" indicator
inside :meth:`MainWindow.refresh`. We do **not** re-show this widget
on those paths — it would be jarring to lose the main window every
time the user pressed F5.

Design choice: a custom ``QWidget`` (not Qt's built-in
``QSplashScreen``) because we want a structured layout — logo,
title, tagline, status line, indeterminate bar — rather than text
painted on top of a pixmap. ``QSplashScreen`` is pixmap-centric; for
anything richer than "image + one message line" a plain QWidget with
``Qt.SplashScreen`` window flag is clearer and easier to evolve."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QProgressBar, QVBoxLayout, QWidget,
)

from .app_icon import app_logo_pixmap


# Logo rendered at 96 logical px — large enough to read as the
# brand without dominating a 460-wide splash. ``app_logo_pixmap``
# tags the QPixmap with DPR=2.0, so this stays sharp on HiDPI.
_LOGO_SIZE = 96

# Splash dimensions — chosen to give the logo + headline + status
# row comfortable breathing room without growing into a "loading
# screen". Kept modest so it doesn't dominate the user's screen.
_SPLASH_W = 460
_SPLASH_H = 280

# Scoped stylesheet rather than app-global — same convention as the
# rest of this codebase (see _TAB_STYLE in editor_panel.py,
# _SKILL_LIST_STYLE in skill_list.py). Selectors use objectName so
# the rules don't cascade outside the splash.
_SPLASH_STYLE = """
QFrame#splashCard {
    background: #ffffff;
    border: 1px solid #c8d0dc;
    border-radius: 8px;
}
QLabel#splashTitle {
    color: #1a1a1a;
    font-size: 18pt;
    font-weight: 600;
}
QLabel#splashTagline {
    color: #777777;
    font-size: 10pt;
}
QLabel#splashStatus {
    color: #4a4a4a;
    font-size: 10pt;
}
QProgressBar#splashBar {
    background: #eef2f8;
    border: 1px solid #d0d7e2;
    border-radius: 4px;
    height: 8px;
}
QProgressBar#splashBar::chunk {
    background: #6b87c2;
    border-radius: 4px;
}
"""


class SplashWindow(QWidget):
    """Frameless centered splash shown during the initial skill scan.

    Use:

    .. code-block:: python

        splash = SplashWindow()
        splash.show()
        QApplication.processEvents()  # force first paint
        # ... do work, calling splash.set_status(...) between phases ...
        splash.close()

    Window flags:

    * ``Qt.SplashScreen`` — frameless, no taskbar entry, doesn't
      activate / steal focus from windows shown after it closes.
      This is the canonical "splash" flag and is exactly what Qt's
      own ``QSplashScreen`` sets internally.
    * ``Qt.WindowStaysOnTopHint`` — paired with ``SplashScreen`` to
      keep the splash above any window that gets created while it's
      visible (notably MainWindow, which we construct *before*
      closing the splash so the heavy ``__init__`` work doesn't
      leak into the visible-window phase).

    No ``WA_DeleteOnClose`` — main.py holds the reference until
    after ``close()``, and Python's reference counting cleans it up
    when ``main()`` returns. Adding ``DeleteOnClose`` would race
    with main.py's local variable lifetime; leaving it off is the
    simpler and safer default for a splash."""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.SplashScreen | Qt.WindowStaysOnTopHint,
        )
        self.setFixedSize(_SPLASH_W, _SPLASH_H)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # Outer transparent margin around the card so the rounded
        # corners and border show against the desktop, not against
        # the splash widget's own background. Qt window-level
        # rounding requires WA_TranslucentBackground + a custom
        # paintEvent — the simpler "card-inside-a-flat-widget"
        # approach gets us most of the look for none of the cost.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("splashCard")
        card.setStyleSheet(_SPLASH_STYLE)
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 28, 28, 22)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignHCenter)

        logo = QLabel()
        logo.setPixmap(app_logo_pixmap(_LOGO_SIZE))
        logo.setAlignment(Qt.AlignHCenter)
        layout.addWidget(logo)

        title = QLabel("Claude Skills Manager")
        title.setObjectName("splashTitle")
        title.setAlignment(Qt.AlignHCenter)
        layout.addWidget(title)

        tagline = QLabel("Browse, edit, and test your Claude Code skills")
        tagline.setObjectName("splashTagline")
        tagline.setAlignment(Qt.AlignHCenter)
        layout.addWidget(tagline)

        # Status row + indeterminate bar. The bar uses the same
        # ``range=(0, 0)`` "marquee" trick as the main window's
        # status-bar busy_bar — Qt animates a sliding fill as long
        # as the event loop ticks, which gives the user a visible
        # cue that work is happening even when we can't predict
        # the scan duration. Caller is expected to call
        # ``QApplication.processEvents()`` between phases for the
        # animation to actually advance.
        self._status = QLabel("Starting…")
        self._status.setObjectName("splashStatus")
        self._status.setAlignment(Qt.AlignHCenter)
        layout.addWidget(self._status)

        bar = QProgressBar()
        bar.setObjectName("splashBar")
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        layout.addWidget(bar)

        self._center_on_screen()

    def set_status(self, text: str) -> None:
        """Update the status line and pump the event loop once so
        the new text actually paints before the next sync step.

        Without the ``processEvents`` pump, status updates emitted
        between two back-to-back synchronous scan calls would
        coalesce — the user would see "Starting…" jump straight to
        the final state, missing the intermediate phases. One
        ``processEvents`` per status flip is enough; we don't need
        a timer or animation."""
        self._status.setText(text)
        QApplication.processEvents()

    def _center_on_screen(self) -> None:
        """Center on the primary screen's *available* geometry —
        i.e., the area minus the taskbar / dock. ``primaryScreen``
        can return ``None`` very early during QApplication startup
        on some platforms; fall back to a fixed top-left position
        so we never crash mid-launch over cosmetics."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.move(120, 120)
            return
        geom = screen.availableGeometry()
        x = geom.x() + (geom.width() - self.width()) // 2
        y = geom.y() + (geom.height() - self.height()) // 2
        self.move(x, y)
