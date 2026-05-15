"""Shared QSS constants used across UI panels.

Per the CLAUDE.md convention "Stylesheets scoped per widget", these strings
are not applied via ``app.setStyleSheet`` — each panel calls
``widget.setStyleSheet(BUTTON_STYLE)`` on the specific buttons it owns, so
the cascade stays local.

Centralizing the constants here means a single source of truth: tweaking
button hover colour or border radius later is one edit, not six call-site
edits across three files."""

# Explicit button surface + border + hover/pressed/disabled states. Qt's
# default WindowsVista style draws QPushButton with a near-invisible border
# that reads as a textbox; this restores the "clearly clickable" affordance
# without resorting to a global app stylesheet.
BUTTON_STYLE = """
QPushButton {
    background: #f5f5f5;
    border: 1px solid #b0b0b0;
    border-radius: 3px;
    padding: 4px 12px;
    min-height: 22px;
    color: #1a1a1a;
}
QPushButton:hover {
    background: #e8e8e8;
    border-color: #888888;
}
QPushButton:pressed {
    background: #d0d0d0;
    border-color: #555555;
}
QPushButton:disabled {
    background: #f5f5f5;
    color: #999999;
    border-color: #d0d0d0;
}
"""
