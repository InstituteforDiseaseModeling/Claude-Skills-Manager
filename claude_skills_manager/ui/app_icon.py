"""Programmatic application logo.

Three-shapes composite (indigo circle + green rounded square + amber
diamond) in the same palette as the per-skill-type badges in
``skill_list.py``. Reusing the palette keeps the logo visually tied to
the app's identity — the icon literally *is* "skills from three sources."

Painted programmatically rather than shipped as a PNG so the app stays
zero-asset and the logo renders crisply at any size. Two public entry
points:

* ``app_icon()`` — multi-size ``QIcon`` for ``setWindowIcon``. Provides
  pixmaps at 16 / 32 / 48 / 64 / 128 / 256 px so Windows can pick the
  best pre-rendered size for each surface (title bar / taskbar /
  Alt+Tab / HiDPI scale) without runtime scaling artefacts.
* ``app_logo_pixmap(logical_size)`` — single ``QPixmap`` for in-window
  use (toolbar, dialogs, splash). Painted at 2x physical size with
  ``setDevicePixelRatio(2.0)`` so the logo stays sharp on HiDPI
  displays — same trick as ``skill_list._paint_type_icon``.

Both layer over the same ``_paint_physical(size)`` helper so the shape
geometry has exactly one source of truth."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygon


# Per-type palette, kept parallel to ``skill_list._TYPE_PAINT``. We don't
# import from skill_list to avoid a UI-internal circular dependency
# between two leaf modules; the trade is that a future palette change
# requires updating both. Two constants in two files is a small price
# for keeping the logo module standalone (see DESIGN.md §7.21).
_GLOBAL_COLOR  = "#5350a3"   # indigo  — Global skills
_PROJECT_COLOR = "#2e8b57"   # green   — Project skills
_PLUGIN_COLOR  = "#d97706"   # amber   — Plugin skills

# Sizes Windows commonly requests for window icons. Painting each one
# explicitly (rather than scaling a single hi-res master) avoids the
# blurriness that runtime downscaling introduces at the smallest sizes.
_ICON_SIZES = (16, 32, 48, 64, 128, 256)

# DPR for in-window pixmaps. Hardcoded at 2.0 to match the existing
# convention in ``skill_list._paint_type_icon``: paint at 2x physical
# size, tag the pixmap as DPR=2.0, and Qt treats it as a logical-size
# image with HiDPI source detail. Crisp on 1x and 2x displays; very
# slightly blurry on 1.5x (rare on modern hardware).
_LOGO_DPR = 2.0


def app_icon() -> QIcon:
    """Return the composite app logo as a multi-size ``QIcon``.

    Must be called *after* a ``QApplication`` exists — ``QPixmap``
    construction requires the GUI subsystem to be initialised."""
    icon = QIcon()
    for size in _ICON_SIZES:
        icon.addPixmap(_paint_physical(size))
    return icon


def write_logo_ico(path) -> bool:
    """Save the composite logo as an ICO file to the given path.

    Used on Windows to give the shell a concrete on-disk icon resource
    to associate with our AppUserModelID via
    ``System.AppUserModel.RelaunchIconResource`` — without this, the
    taskbar shows a blank/generic icon for run-from-source apps even
    when ``WM_SETICON`` has been called on the window. See §7.22.

    Returns True on success, False if Qt's ICO writer is unavailable
    or the path can't be written. Windows-only callers should treat
    the False case as "fall back to whatever icon the OS picks" —
    not fatal, just cosmetic."""
    pix = _paint_physical(256)
    return bool(pix.save(str(path), "ICO"))


def app_logo_pixmap(logical_size: int) -> QPixmap:
    """Return a single ``QPixmap`` of the logo at the given logical size,
    sized for in-window use (e.g., a ``QLabel`` in the toolbar).

    Painted at ``logical_size * _LOGO_DPR`` physical pixels with the
    pixmap tagged as DPR=2.0, so Qt renders it at the requested logical
    size while preserving HiDPI source detail."""
    physical = int(round(logical_size * _LOGO_DPR))
    pix = _paint_physical(physical)
    pix.setDevicePixelRatio(_LOGO_DPR)
    return pix


def _paint_physical(size: int) -> QPixmap:
    """Render the three-shapes composite at the given *physical* pixel
    size. Geometry is expressed as fractions of the canvas so the layout
    scales cleanly across sizes; integer-rounding before each draw keeps
    small renderings (16 px) sharp on pixel boundaries.

    Layout (triangle, top-heavy):
        - Circle  (Global):  top center
        - Square  (Project): bottom left
        - Diamond (Plugin):  bottom right
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)

    # Circle — Global (top center)
    painter.setBrush(QColor(_GLOBAL_COLOR))
    cx, cy, r = 0.50 * size, 0.28 * size, 0.18 * size
    painter.drawEllipse(int(cx - r), int(cy - r), int(2 * r), int(2 * r))

    # Rounded square — Project (bottom left)
    painter.setBrush(QColor(_PROJECT_COLOR))
    sx, sy, ss = 0.30 * size, 0.70 * size, 0.32 * size
    radius = ss * 0.18
    painter.drawRoundedRect(
        int(sx - ss / 2), int(sy - ss / 2), int(ss), int(ss), radius, radius)

    # Diamond — Plugin (bottom right)
    painter.setBrush(QColor(_PLUGIN_COLOR))
    dx, dy, dr = 0.70 * size, 0.70 * size, 0.18 * size
    painter.drawPolygon(QPolygon([
        QPoint(int(dx),       int(dy - dr)),
        QPoint(int(dx + dr),  int(dy)),
        QPoint(int(dx),       int(dy + dr)),
        QPoint(int(dx - dr),  int(dy)),
    ]))

    painter.end()
    return pix
