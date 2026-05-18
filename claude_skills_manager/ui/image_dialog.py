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
from PySide6.QtGui import QImageReader, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QDialog, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)


def _supported_image_exts() -> frozenset[str]:
    """Return the set of file extensions Qt's currently-loaded image
    plugins can decode (e.g. ``{'.png', '.jpg', '.svg', ...}``).

    Computed at call time, not module-import time, because the SVG
    plugin registers itself lazily on ``import QtSvg`` above — so the
    answer depends on whether that import succeeded. Using Qt's own
    list (instead of a hardcoded set) keeps sibling discovery in
    lock-step with what ``_load`` will actually accept: a folder
    listing that includes an extension Qt can't open would let the
    user page to an image that immediately fails."""
    return frozenset(
        "." + bytes(fmt).decode("ascii").lower()
        for fmt in QImageReader.supportedImageFormats()
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

        # Discover sibling images in the same folder so the user can
        # page through them with the toolbar arrows / Left-Right keys.
        # Computed once at __init__: if the user adds or removes files
        # mid-session, they need to close + reopen to pick up changes.
        # Worth it — re-scanning on every keystroke would race with
        # user navigation (and a network folder could stall the UI).
        self._siblings: list[Path] = []
        self._index: int = 0
        self._build_siblings(path)

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
        # Navigation arrows come first — same left-to-right reading order
        # as the keyboard shortcuts (Left = prev, Right = next), so users
        # don't have to remember which is which.
        self._prev_btn = QPushButton("◀")
        self._next_btn = QPushButton("▶")
        self._prev_btn.setToolTip(
            "Previous image in this folder (Left arrow / Page Up)")
        self._next_btn.setToolTip(
            "Next image in this folder (Right arrow / Page Down)")
        self._prev_btn.clicked.connect(self.show_prev)
        self._next_btn.clicked.connect(self.show_next)
        # Position label between the arrows — Apple Photos / file-explorer
        # style "3 / 17" so the user knows where they are in the run.
        self._pos_label = QLabel("—")
        self._pos_label.setStyleSheet("color: #444; padding: 0 6px;")
        self._pos_label.setMinimumWidth(56)
        self._pos_label.setAlignment(Qt.AlignCenter)
        for btn in (self._prev_btn, self._next_btn):
            btn.setFixedWidth(36)
        bar.addWidget(self._prev_btn)
        bar.addWidget(self._pos_label)
        bar.addWidget(self._next_btn)
        bar.addSpacing(12)

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

        # Dialog-level shortcuts for navigation. Using QShortcut instead
        # of keyPressEvent because QGraphicsView (the focus widget once
        # the user pans / clicks the image) consumes plain Left/Right
        # arrow events to scroll its viewport — so a keyPressEvent
        # override on the dialog never sees them. QShortcut bypasses
        # focus-based key routing and fires at the dialog level.
        for keys, slot in (
            ((Qt.Key_Left, Qt.Key_PageUp), self.show_prev),
            ((Qt.Key_Right, Qt.Key_PageDown), self.show_next),
        ):
            for key in keys:
                QShortcut(QKeySequence(key), self).activated.connect(slot)

        if not self._load(self._siblings[self._index]):
            # Initial path failed to load — defer reject so the warning
            # box actually paints before the dialog disappears. (Same
            # deferral the legacy _load() did inline; pulled out here
            # so navigation failures can choose a different policy.)
            QTimer.singleShot(0, self.reject)
            return
        self._update_nav_state()

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

    def show_prev(self) -> None:
        """Page to the previous image in the folder. No-op at index 0."""
        self._navigate_to(self._index - 1)

    def show_next(self) -> None:
        """Page to the next image in the folder. No-op at last index."""
        self._navigate_to(self._index + 1)

    # ----------------------------------------------------------- internals
    def _build_siblings(self, path: Path) -> None:
        """Populate ``self._siblings`` with image files in ``path.parent``,
        sorted by case-insensitive filename so the order matches what the
        OS file explorer typically shows. ``self._index`` is set to
        ``path``'s position in that list.

        Defensive fallbacks: if the directory can't be listed (permission
        error, network mount went away) or contains no recognizable
        images, the list collapses to ``[path]`` so the dialog still
        works — just with nav disabled."""
        try:
            entries = list(path.parent.iterdir())
        except OSError:
            self._siblings = [path]
            self._index = 0
            return
        exts = _supported_image_exts()
        images = sorted(
            (p for p in entries
             if p.is_file() and p.suffix.lower() in exts),
            key=lambda p: p.name.lower(),
        )
        if not images:
            self._siblings = [path]
            self._index = 0
            return
        try:
            resolved = path.resolve()
            idx = next(i for i, p in enumerate(images)
                       if p.resolve() == resolved)
        except (StopIteration, OSError):
            # Opened image isn't in the sibling list (unusual extension,
            # symlink resolution mismatch, etc.). Stitch it onto the
            # front so the dialog still loads it.
            images.insert(0, path)
            idx = 0
        self._siblings = images
        self._index = idx

    def _navigate_to(self, new_index: int) -> None:
        """Page to ``new_index`` in the siblings list. Out-of-bounds is a
        no-op (the buttons are already disabled at boundaries, but the
        check is here too for the keyboard-shortcut path, which doesn't
        consult button state). On load failure the previous image stays
        visible and ``_index`` doesn't advance — failure mode the
        sibling-discovery extension filter is supposed to prevent, but
        e.g. a corrupted PNG would still trip it."""
        if not (0 <= new_index < len(self._siblings)):
            return
        target = self._siblings[new_index]
        if not self._load(target):
            return  # _load already showed the warning; keep current image
        self._index = new_index
        self.setWindowTitle(target.name)
        self._update_nav_state()

    def _update_nav_state(self) -> None:
        """Refresh the Prev/Next enabled state and the ``N / M`` label.
        Called after every navigation and once from ``__init__``. Both
        buttons are disabled when there's only one image (collapses to
        a viewer with no nav surface)."""
        total = len(self._siblings)
        self._prev_btn.setEnabled(self._index > 0)
        self._next_btn.setEnabled(self._index < total - 1)
        if total <= 1:
            self._pos_label.setText("—")
        else:
            self._pos_label.setText(f"{self._index + 1} / {total}")

    def _load(self, path: Path) -> bool:
        """Load ``path`` into the scene. Returns True on success, False on
        failure (in which case a warning has been shown and the scene
        retains whatever was previously displayed). The caller decides
        what to do on False — the initial-load path rejects the dialog
        outright, navigation just stays on the current image."""
        pix = QPixmap(str(path))
        if pix.isNull():
            QMessageBox.warning(
                self, "Cannot display",
                f"{path.name} could not be loaded as an image. The format "
                f"may not be supported by Qt's bundled image plugins (e.g. "
                f"SVG without QtSvg, animated GIF, corrupt file).")
            return False
        self._pixmap_item.setPixmap(pix)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self._dims_label.setText(f"{pix.width()}×{pix.height()} px")
        # Re-arm fit-mode and refit on the next tick — a navigation
        # carries no expectation that a previous zoom level should apply
        # to a totally different image. Deferred via singleshot so the
        # view's layout has settled (relevant on first show; harmless on
        # subsequent navigations).
        self._fit_mode = True
        QTimer.singleShot(0, self.fit_to_window)
        return True

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
