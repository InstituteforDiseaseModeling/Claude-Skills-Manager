"""Small programmatic UI icons used inside the toolbar / line edits.

Distinct from ``app_icon.py`` which is specifically about the app's
brand logo. This module collects general-purpose stroke-style icons
(magnifying glass, refresh arrow, future: clear buttons, status
indicators, etc.). Same painting convention as the rest of the
codebase: 2x physical canvas + ``setDevicePixelRatio(2.0)`` for HiDPI
sharpness, no asset files."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF,
)


# Cached at module level — QIcon construction needs a QApplication, so
# the cache is populated lazily on first call. Same pattern as the
# per-type icon caches in skill_list.py.
_search_icon_cache: QIcon | None = None
_refresh_icon_cache: QIcon | None = None


def search_icon() -> QIcon:
    """Return (and lazily build + cache) a stroked magnifying-glass icon
    for use as the leading icon of a search ``QLineEdit``.

    Neutral grey stroke so the icon reads as a marker without competing
    with the input text or placeholder. Painted at logical 16x16 with
    DPR=2 — Qt's ``QLineEdit`` leading-action position renders icons at
    the input's height minus padding, typically around 14-16 logical px.
    """
    global _search_icon_cache
    if _search_icon_cache is not None:
        return _search_icon_cache

    # Paint at physical coords on a plain 32x32 pixmap, THEN tag DPR=2.0.
    # Setting DPR before painting would flip QPainter into logical
    # (16x16) coords and clip the icon to a quarter-shape — see §7.24
    # for the diagnosis chain that uncovered this in the existing
    # type-icon painter.
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)

    # Stroke style: muted grey with rounded caps so the handle's
    # endpoints don't look chopped at small sizes.
    pen = QPen(QColor("#888888"))
    pen.setWidthF(2.4)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    # Lens (circle outline). Center at physical (13, 13) with radius 8 —
    # bounding box (5, 5) to (21, 21), centered slightly toward top-left
    # so the handle extends to a roughly-square overall composition.
    painter.drawEllipse(QPointF(13, 13), 8, 8)

    # Handle (diagonal line) from the lens edge at ~45° to bottom-right.
    # (19, 19) is just past the 4 o'clock position on the lens; (28, 28)
    # leaves a 4 px margin from the canvas edge for breathing room.
    painter.drawLine(QPointF(19, 19), QPointF(28, 28))

    painter.end()
    pix.setDevicePixelRatio(2.0)

    _search_icon_cache = QIcon(pix)
    return _search_icon_cache


def refresh_icon() -> QIcon:
    """Return (and lazily build + cache) the standard clockwise-arrow
    refresh icon for the toolbar's Refresh button.

    Shape conventions (matching Material / Fluent / modern web UIs):

    * A ~270° clockwise arc with a ~90° gap at the top — the gap reads
      as the missing piece of the rotation, so the user's eye fills in
      the implied motion.
    * A filled triangular arrowhead at the **start** of the arc
      (upper-right, ~1:30 clock position) pointing along the clockwise
      tangent (down-right). The arrowhead at the start says "this is
      where the rotation begins" — the "go around again" gesture.

    Painted at logical 16x16 with DPR=2 — Qt's ``QPushButton`` uses
    16x16 icons by default on Windows. Stroke matches the colors of
    the surrounding toolbar text; the arrowhead is filled so it reads
    crisply at small sizes (a hollow triangle gets spindly fast)."""
    global _refresh_icon_cache
    if _refresh_icon_cache is not None:
        return _refresh_icon_cache

    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)

    # Match the muted-dark grey used elsewhere in the toolbar so the
    # icon coexists visually with the text labels and search-box stroke.
    color = QColor("#444444")
    pen = QPen(color)
    pen.setWidthF(2.4)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    # Arc geometry. Center (16, 16), radius 10 → bounding box (6,6,20,20).
    # Qt angles are in 1/16 degree, 0° = East (3 o'clock), positive = CCW
    # (standard math convention). To draw a clockwise arc starting at
    # 45° and covering 270°, use start=45°*16 and span = -270°*16.
    start_deg = 45.0
    span_deg = -270.0
    rect = QRectF(6, 6, 20, 20)
    painter.drawArc(rect, int(start_deg * 16), int(span_deg * 16))

    # Arrowhead at the start of the arc (θ = 45°). The "tip" sits on
    # the circle; the arrowhead extends backward (toward the center)
    # so its base is INSIDE the arc, not floating off the edge.
    cx, cy, r = 16.0, 16.0, 10.0
    theta = math.radians(start_deg)
    # Position on the circle. Qt uses screen coords (Y down), so we
    # flip the sin term: standard math (cos θ, sin θ) → (cos θ, -sin θ).
    tip = QPointF(cx + r * math.cos(theta), cy - r * math.sin(theta))
    # Clockwise tangent direction in Qt screen coords:
    # d/dθ of (cx + r cos θ, cy - r sin θ) gives (-r sin θ, -r cos θ)
    # for CCW; flip sign for CW → (r sin θ, r cos θ). Unit length is r.
    fx = math.sin(theta)   # unit forward (CW), x component
    fy = math.cos(theta)   # unit forward (CW), y component
    # Perpendicular to forward (rotated 90° CW in screen coords) → wings
    # spread on either side of the back point.
    px = fy
    py = -fx
    arrow_len = 5.0    # how far back the base sits from the tip
    arrow_half = 3.2   # half-width of the base
    back_x = tip.x() - arrow_len * fx
    back_y = tip.y() - arrow_len * fy
    wing1 = QPointF(back_x + arrow_half * px, back_y + arrow_half * py)
    wing2 = QPointF(back_x - arrow_half * px, back_y - arrow_half * py)

    # Fill the triangle (no outline) so it reads as a solid arrowhead.
    # A stroked triangle looks spindly at 16px logical size.
    painter.setBrush(QBrush(color))
    painter.setPen(Qt.NoPen)
    painter.drawPolygon(QPolygonF([tip, wing1, wing2]))

    painter.end()
    pix.setDevicePixelRatio(2.0)

    _refresh_icon_cache = QIcon(pix)
    return _refresh_icon_cache
