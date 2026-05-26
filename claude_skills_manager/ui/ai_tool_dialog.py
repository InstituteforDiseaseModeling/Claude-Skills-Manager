"""Modal dialog rendering one AITool record.

Triggered from the Resource menu in the main window. One class,
parameterized by an ``AITool`` — not one class per tool, since the
table has ~27 entries and they all share the same layout.

Hyperlink mechanics use ``QLabel.setOpenExternalLinks(True)``, which
routes link activations through ``QDesktopServices.openUrl`` — the
same path the rest of the app uses for "open in OS file manager"
(see ``main_window._open_log_folder``). Keeps the dependency surface
flat: no QWebEngineView, no extra widgets.
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget,
)

from ..ai_tools import AITool
from ._styles import BUTTON_STYLE


class AIToolDialog(QDialog):
    def __init__(self, tool: AITool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tool = tool
        self.setWindowTitle(f"{tool.name} — Resource Info")
        self.setModal(True)
        self.resize(520, 320)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        name_label = QLabel(
            f"<h2 style='margin:0;'>{html.escape(self._tool.name)}</h2>")
        name_label.setTextFormat(Qt.RichText)
        layout.addWidget(name_label)

        layout.addWidget(self._link_label("Main Website", self._tool.main_url))
        layout.addWidget(self._link_label("Documentation", self._tool.docs_url))

        summary_header = QLabel(
            "<p style='margin:8px 0 0 0; color:#444;'><b>Summary</b></p>")
        summary_header.setTextFormat(Qt.RichText)
        layout.addWidget(summary_header)

        summary = QLabel(self._tool.summary)
        summary.setWordWrap(True)
        summary.setAlignment(Qt.AlignTop)
        summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(summary, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        for btn in buttons.buttons():
            btn.setStyleSheet(BUTTON_STYLE)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @staticmethod
    def _link_label(prefix: str, url: str) -> QLabel:
        """One labelled-hyperlink row. ``url`` is escaped to defeat
        HTML injection from a maliciously crafted resource file —
        belt-and-braces, since the source is packaged with the app,
        but the cost is one ``html.escape`` call."""
        if url:
            safe = html.escape(url, quote=True)
            body = (f"<b>{prefix}:</b> "
                    f"<a href='{safe}'>{safe}</a>")
        else:
            body = f"<b>{prefix}:</b> <i style='color:#888;'>(not listed)</i>"
        lbl = QLabel(body)
        lbl.setTextFormat(Qt.RichText)
        lbl.setOpenExternalLinks(True)
        lbl.setTextInteractionFlags(Qt.TextBrowserInteraction)
        lbl.setWordWrap(True)
        return lbl
