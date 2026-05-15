"""Bottom-middle panel — at-a-glance metadata for the selected skill's SKILL.md.

Sits beneath the file tree and refreshes whenever a skill is selected. Token
count is an approximation (chars / `_CHARS_PER_TOKEN`) since Claude's
tokenizer isn't bundled locally — exact counts would require the anthropic
SDK or a remote API call, and we keep the dependency footprint minimal."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..models import Skill
from ..skill_md import estimated_token_count
from ..skill_settings import (
    BINARY_STATES, STATE_OFF, STATE_ON,
)
from ._styles import BUTTON_STYLE


class SkillInfoPanel(QWidget):
    """Summary of the selected skill's SKILL.md file plus its enable/disable
    toggle. The metadata rows are read-only; the State row offers a binary
    toggle for Global/Project skills with a binary state, and a read-only
    label otherwise (plugin-controlled or non-binary override)."""

    # Emitted when the user clicks Enable/Disable. MainWindow handles the
    # actual settings.local.json write and refresh.
    state_change_requested = Signal(object, str)  # (Skill, "on" | "off")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_skill: Skill | None = None

        # ---- Title bar: "SKILL.md" label and Enable/Disable buttons ----
        # The toggle lives at the top of the panel, on the same row as the
        # title, so it's reachable without scanning down through the
        # metadata. Status text (current state, plugin / read-only notes)
        # still appears in the form's State row below.
        title_label = QLabel("SKILL.md")
        title_font = QFont()
        title_font.setBold(True)
        title_label.setFont(title_font)

        self._enable_btn = QPushButton("Enable")
        self._enable_btn.setStyleSheet(BUTTON_STYLE)
        self._enable_btn.setFixedWidth(72)
        self._enable_btn.clicked.connect(lambda: self._request_state(STATE_ON))
        self._disable_btn = QPushButton("Disable")
        self._disable_btn.setStyleSheet(BUTTON_STYLE)
        self._disable_btn.setFixedWidth(72)
        self._disable_btn.clicked.connect(lambda: self._request_state(STATE_OFF))

        title_widget = QWidget()
        # Object-scoped QSS so the grey background applies to the bar only,
        # not to the buttons (which would otherwise lose their native style).
        title_widget.setObjectName("infoTitleBar")
        title_widget.setStyleSheet(
            "QWidget#infoTitleBar { background: #f3f3f3; }")
        title_row = QHBoxLayout(title_widget)
        title_row.setContentsMargins(8, 4, 8, 4)
        title_row.setSpacing(6)
        title_row.addWidget(title_label)
        title_row.addStretch(1)
        title_row.addWidget(self._enable_btn)
        title_row.addWidget(self._disable_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)

        # ---- Form: metadata rows + State text row ----
        self._size_label = QLabel("—")
        self._mtime_label = QLabel("—")
        self._lines_label = QLabel("—")
        self._chars_label = QLabel("—")
        self._tokens_label = QLabel("—")
        self._state_label = QLabel("—")
        self._value_labels: tuple[QLabel, ...] = (
            self._size_label, self._mtime_label, self._lines_label,
            self._chars_label, self._tokens_label,
        )
        # Word-wrap on every value so long content (the State row's
        # "controlled by plugin: …" / "name-only (read-only — edit …)" text)
        # wraps within the column instead of forcing the middle pane wider.
        for lbl in (*self._value_labels, self._state_label):
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        form = QFormLayout()
        form.setContentsMargins(10, 8, 10, 10)
        form.setVerticalSpacing(4)
        form.setHorizontalSpacing(12)
        # AlignTop (not AlignVCenter): when a value wraps to multiple lines
        # the label should pin to the first line, not float between them.
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)
        form.addRow("Size:",        self._size_label)
        form.addRow("Modified:",    self._mtime_label)
        form.addRow("Lines:",       self._lines_label)
        form.addRow("Chars:",       self._chars_label)
        form.addRow("Tokens (≈):",  self._tokens_label)
        form.addRow("State:",       self._state_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(title_widget)
        layout.addWidget(sep)
        layout.addLayout(form)
        layout.addStretch(1)

    # ------------------------------------------------------------- public API
    def show_skill(self, skill: Skill) -> None:
        self._current_skill = skill
        self._render_state(skill)
        path = skill.skill_md_path
        if path is None or not path.is_file():
            # State row stays populated even if SKILL.md vanished — the
            # toggle is about the skill folder, not the file.
            self._size_label.setText("—")
            self._mtime_label.setText("(SKILL.md missing)")
            self._lines_label.setText("—")
            self._chars_label.setText("—")
            self._tokens_label.setText("—")
            return

        try:
            stat = path.stat()
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._size_label.setText("—")
            self._mtime_label.setText(f"(unreadable: {e})")
            self._lines_label.setText("—")
            self._chars_label.setText("—")
            self._tokens_label.setText("—")
            return

        chars = len(text)
        # Empty file → 0 lines. Otherwise count newlines, plus 1 for a final
        # line that lacks a trailing newline (typical of editor output).
        if text:
            lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        else:
            lines = 0
        tokens_est = estimated_token_count(text)

        self._size_label.setText(_format_size(stat.st_size))
        self._mtime_label.setText(
            datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"))
        self._lines_label.setText(f"{lines:,}")
        self._chars_label.setText(f"{chars:,}")
        self._tokens_label.setText(f"{tokens_est:,}")

    def clear(self) -> None:
        self._current_skill = None
        for lbl in self._value_labels:
            lbl.setText("—")
        self._state_label.setText("—")
        self._enable_btn.setEnabled(False)
        self._disable_btn.setEnabled(False)
        self._enable_btn.setToolTip("")
        self._disable_btn.setToolTip("")

    # ----------------------------------------------------------- state helpers
    def _render_state(self, skill: Skill) -> None:
        """Populate the State row from `skill.state` and `skill.plugin_id`."""
        is_plugin = skill.plugin_id is not None
        is_binary = skill.state in BINARY_STATES

        # Default: both buttons off; the branches below re-enable as needed.
        self._enable_btn.setEnabled(False)
        self._disable_btn.setEnabled(False)
        self._enable_btn.setToolTip("")
        self._disable_btn.setToolTip("")

        if is_plugin:
            label = "Enabled" if skill.state == STATE_ON else "Disabled"
            self._state_label.setText(
                f"{label}  (controlled by plugin: {skill.plugin_id})")
            tip = "Plugin skills can't be toggled individually — manage via /plugin"
            self._enable_btn.setToolTip(tip)
            self._disable_btn.setToolTip(tip)
            return

        if not is_binary:
            self._state_label.setText(
                f"{skill.state}  (read-only — edit settings.local.json to change)")
            tip = (f"State is '{skill.state}'. Toggling here would collapse the "
                   f"nuance — edit settings.local.json by hand to change it.")
            self._enable_btn.setToolTip(tip)
            self._disable_btn.setToolTip(tip)
            return

        # Binary, toggleable.
        if skill.state == STATE_ON:
            self._state_label.setText("Enabled")
            self._disable_btn.setEnabled(True)
        else:
            self._state_label.setText("Disabled")
            self._enable_btn.setEnabled(True)

    def _request_state(self, new_state: str) -> None:
        if self._current_skill is None:
            return
        self.state_change_requested.emit(self._current_skill, new_state)


# ----------------------------------------------------------------------- helpers
def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"
