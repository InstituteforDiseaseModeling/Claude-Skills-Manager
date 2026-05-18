"""About dialog — app name, version, brief description, runtime info.

A modal QDialog rather than QMessageBox.about so the layout can include
the app icon next to the text without fighting QMessageBox's fixed
internal structure."""
from __future__ import annotations

import platform
import sys

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import Qt, qVersion
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from .. import __version__ as app_version
from .app_icon import app_logo_pixmap
from ._styles import BUTTON_STYLE


_ABOUT_HTML = """
<h2 style='margin-bottom:2px;'>Claude Skills Manager</h2>
<p style='color:#666; margin-top:0;'>Version {version}</p>
<p>Browse, edit, and toggle Claude Code skills from one window — Global,
Project, and Plugin sources unified, with a markdown-rendered description
preview, an in-app code editor, and a per-skill test runner that shells
out to your installed <code>claude</code> CLI.</p>
<p style='color:#777; font-size:9pt; margin-top:14px;'>
Python {py_version} · PySide6 {pyside_version} · Qt {qt_version}
</p>
"""


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Claude Skills Manager")
        self.setModal(True)
        self.resize(520, 320)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        # Horizontal: logo on the left, rich-text content on the right.
        row = QHBoxLayout()
        row.setSpacing(16)

        logo = QLabel()
        logo.setPixmap(app_logo_pixmap(72))
        logo.setAlignment(Qt.AlignTop)
        row.addWidget(logo, 0, Qt.AlignTop)

        body = QLabel(_ABOUT_HTML.format(
            version=app_version,
            py_version=platform.python_version(),
            pyside_version=pyside_version,
            qt_version=qVersion(),
        ))
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setAlignment(Qt.AlignTop)
        row.addWidget(body, 1)

        layout.addLayout(row, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        for btn in buttons.buttons():
            btn.setStyleSheet(BUTTON_STYLE)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
