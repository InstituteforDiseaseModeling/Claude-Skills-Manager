"""Modal image viewer with Ctrl+wheel zoom (cursor-anchored), drag-pan,
and a toolbar with Fit / 100% / − / + plus keyboard shortcuts.

Implementation uses Qt's Graphics View framework: a ``QGraphicsScene``
holds a single ``QGraphicsPixmapItem``, and ``QGraphicsView`` renders the
scene with affine transforms. Scaling is a matrix-multiply on the view,
not a re-rasterisation of the source ``QPixmap`` — so zoom stays smooth
at any factor and the source pixels never lose precision."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

# Importing QtSvg here registers Qt's SVG image plugin so QPixmap can load
# .svg files. Without it, SVG paths return null pixmaps. The import is
# defensive: if the user's PySide6 build is missing QtSvg the dialog still
# works for raster formats, SVG just gracefully fails to "Cannot display".
try:
    from PySide6 import QtSvg  # noqa: F401
except ImportError:
    pass


class _ZoomPanView(QGraphicsView):
    """Internal view that implements Ctrl+wheel zoom (cursor-anchored) and
    delegates drag-pan to the inherited ``ScrollHandDrag`` mode. Plain
    (un-modified) wheel events fall through so a trackpad can still scroll
    the view normally without an unintentional zoom."""

    zoom_changed = Signal()  # emitted by zoom_by — any user-initiated zoom

    # 1.15 per wheel notch: ~5 notches doubles the visible size — a
    # comfortable log ramp. Tighter (1.05) feels sluggish; looser (1.5)
    # skips past the level the user wanted.
    _ZOOM_STEP = 1.15

    # Bracket the absolute scale so the user can't strand themselves at
    # an unrecoverably small or huge zoom level.
    _MIN_SCALE = 0.05    # 5% — fits a 4K image into a small window
    _MAX_SCALE = 32.0    # 3200% — pixel-level inspection of small icons

    def zoom_by(self, factor: float) -> None:
        # Clamp the *resulting* scale, not the multiplicative factor —
        # otherwise repeated zooms drift past the bounds incrementally.
        next_scale = self.transform().m11() * factor
        if next_scale < self._MIN_SCALE or next_scale > self._MAX_SCALE:
            return
        self.scale(factor, factor)
        self.zoom_changed.emit()

    def zoom_centered(self, factor: float) -> None:
        """Zoom anchored on the view's centre, then restore the previous
        anchor. For toolbar buttons / keyboard shortcuts the cursor is
        somewhere outside the view — AnchorUnderMouse would jump the image
        to wherever the mouse was last, which is visually disorienting."""
        prev = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        self.zoom_by(factor)
        self.setTransformationAnchor(prev)

    def wheelEvent(self, event):  # noqa: N802 — Qt naming
        if not (event.modifiers() & Qt.ControlModifier):
            # Plain wheel: let Qt's default scroll the view via scrollbars.
            super().wheelEvent(event)
            return
        # Ctrl+wheel: cursor-anchored zoom. ``angleDelta()`` returns eighths
        # of a degree; sign tells direction (positive = wheel-up = zoom in).
        factor = (self._ZOOM_STEP if event.angleDelta().y() > 0
                  else 1.0 / self._ZOOM_STEP)
        self.zoom_by(factor)
        event.accept()


class ImageDialog(QDialog):
    """Modal image viewer.

    Toolbar:   Fit · 100% · − · +    +  zoom %  +  source dimensions
    Mouse:     Ctrl+wheel zoom (anchored on cursor),  drag to pan
    Keyboard:  +/=  zoom in    -  zoom out    0  100%    F  fit    Esc  close
    """

    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(path.name)
        self.resize(900, 700)

        # Scene + item: the pixmap is owned by an item placed in a scene,
        # which is rendered by the view. Single-image case, but the whole
        # framework still buys us GPU-smooth transforms, scrollbars, and
        # built-in pan-via-drag for free — net savings vs hand-rolling.
        self._scene = QGraphicsScene(self)
        self._pixmap_item = QGraphicsPixmapItem()
        self._scene.addItem(self._pixmap_item)

        self._view = _ZoomPanView(self._scene)
        # Cursor-anchored zoom by default; ``zoom_centered`` swaps temporarily
        # for toolbar/keyboard zooms so the image doesn't jump.
        self._view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._view.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self._view.setDragMode(QGraphicsView.ScrollHandDrag)
        # SmoothPixmapTransform = bilinear filtering on scaled pixmaps;
        # without it, zoomed pixels render with hard nearest-neighbor edges
        # (acceptable for pixel-art icons, ugly for photos and screenshots).
        self._view.setRenderHints(
            QPainter.SmoothPixmapTransform | QPainter.Antialiasing)
        self._view.zoom_changed.connect(self._on_user_zoom)

        # ---- Toolbar ----
        bar = QHBoxLayout()
        bar.setSpacing(4)
        fit_btn    = QPushButton("Fit")
        actual_btn = QPushButton("100%")
        out_btn    = QPushButton("−")
        in_btn     = QPushButton("+")
        for btn in (fit_btn, actual_btn, out_btn, in_btn):
            btn.setFixedWidth(48)
            bar.addWidget(btn)
        fit_btn.clicked.connect(self.fit_to_window)
        actual_btn.clicked.connect(self.reset_zoom)
        out_btn.clicked.connect(self.zoom_out)
        in_btn.clicked.connect(self.zoom_in)

        self._zoom_label = QLabel("—")
        self._dims_label = QLabel("—")
        for lbl in (self._zoom_label, self._dims_label):
            lbl.setStyleSheet("color: #444; padding: 0 6px;")
        bar.addSpacing(12)
        bar.addWidget(self._zoom_label)
        bar.addSpacing(8)
        bar.addWidget(self._dims_label)
        bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(bar)
        layout.addWidget(self._view)

        # `_fit_mode` keeps the image refitted as the dialog resizes — the
        # flag flips off as soon as the user does any explicit zoom, so
        # their manual zoom level survives subsequent window resizes.
        self._fit_mode = True

        self._load(path)

    # ----------------------------------------------------------- public API
    def fit_to_window(self) -> None:
        """Scale-to-fit with aspect ratio preserved, and arm fit-mode so the
        image refits on subsequent dialog resizes."""
        self._view.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
        self._fit_mode = True
        self._update_status()

    def reset_zoom(self) -> None:
        """Show the image at 1:1, recentered on the pixmap centre. After
        resetTransform() the scrollbars are at (0,0); centerOn ensures the
        image is in view rather than potentially scrolled to a corner."""
        self._view.resetTransform()
        self._view.centerOn(self._pixmap_item)
        self._fit_mode = False
        self._update_status()

    def zoom_in(self) -> None:
        self._view.zoom_centered(_ZoomPanView._ZOOM_STEP)

    def zoom_out(self) -> None:
        self._view.zoom_centered(1.0 / _ZoomPanView._ZOOM_STEP)

    # ----------------------------------------------------------- internals
    def _load(self, path: Path) -> None:
        pix = QPixmap(str(path))
        if pix.isNull():
            QMessageBox.warning(
                self, "Cannot display",
                f"{path.name} could not be loaded as an image. The format "
                f"may not be supported by Qt's bundled image plugins (e.g. "
                f"SVG without QtSvg, animated GIF, corrupt file).")
            # Defer reject() to the next event-loop tick so the warning
            # actually shows before the dialog disappears.
            QTimer.singleShot(0, self.reject)
            return
        self._pixmap_item.setPixmap(pix)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self._dims_label.setText(f"{pix.width()}×{pix.height()} px")
        # Defer the initial fit until the view has been laid out — fitInView
        # before the dialog is shown would use the placeholder size and
        # produce a wrong fit. Singleshot(0) defers to the next tick after
        # the layout system has settled.
        QTimer.singleShot(0, self.fit_to_window)

    def _on_user_zoom(self) -> None:
        """Slot for view.zoom_changed — any user-initiated zoom turns off
        fit-mode so resizes don't override the user's choice."""
        self._fit_mode = False
        self._update_status()

    def _update_status(self) -> None:
        scale = self._view.transform().m11()
        self._zoom_label.setText(f"Zoom: {scale * 100:.0f}%")

    # --------------------------------------------------------- key + resize
    def keyPressEvent(self, event):  # noqa: N802 — Qt naming
        key = event.key()
        if key in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_in()
        elif key in (Qt.Key_Minus, Qt.Key_Underscore):
            self.zoom_out()
        elif key == Qt.Key_0:
            self.reset_zoom()
        elif key == Qt.Key_F:
            self.fit_to_window()
        elif key == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):  # noqa: N802 — Qt naming
        super().resizeEvent(event)
        if self._fit_mode and not self._pixmap_item.pixmap().isNull():
            # Stay fitted as the user resizes the window. The flag was set
            # by fit_to_window() and gets cleared by any explicit zoom, so
            # this only refits when the user is currently in "fit" view.
            self._view.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
            self._update_status()
