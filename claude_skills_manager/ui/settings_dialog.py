"""Modal settings dialog — model / API key / test timeout.

Reads and writes through :mod:`claude_skills_manager.app_settings` so
the dialog has no direct knowledge of the storage backend (QSettings
today, could be a JSON file or env vars later)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QSpinBox, QVBoxLayout, QWidget,
)

from .. import app_settings
from ._icons import eye_icon, eye_slash_icon
from ._styles import BUTTON_STYLE


class SettingsDialog(QDialog):
    """Modal settings dialog. ``exec()`` returns ``QDialog.Accepted``
    when the user clicks OK (settings persisted) or
    ``QDialog.Rejected`` on Cancel (no writes)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(440, 240)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        intro = QLabel(
            "Defaults are 'let <code>claude</code> decide'. Override any field "
            "to pin a value across runs."
        )
        intro.setTextFormat(Qt.RichText)
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#555;")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        # Model: editable combobox so power users can type a model
        # name we don't ship in KNOWN_MODELS. Empty selection means
        # "use claude's default."
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        for m in app_settings.KNOWN_MODELS:
            self.model_combo.addItem(m or "(default — let claude pick)", m)
        self.model_combo.setToolTip(
            "Passed as `--model` to `claude`. Leave at default to let "
            "claude pick.")
        form.addRow("Model:", self.model_combo)

        # API key: password-style so shoulder-surfers don't catch it.
        # Empty means "inherit from environment / claude's own auth."
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText(
            "(empty — inherit from environment / claude auth)")
        self.api_key_edit.setToolTip(
            "If set, exposed as ANTHROPIC_API_KEY when invoking `claude`. "
            "Leave empty to use whatever auth `claude` is already "
            "configured with.")
        # Reveal/hide toggle on the right edge of the field. Icon
        # convention: eye = "currently hidden, click to show"; eye-
        # with-slash = "currently visible, click to hide" (same gesture
        # GitHub / iOS / Windows use). The icon represents what
        # clicking will DO, not the current state.
        self._reveal_action = self.api_key_edit.addAction(
            eye_icon(), QLineEdit.TrailingPosition)
        self._reveal_action.setToolTip("Show API key")
        self._reveal_action.triggered.connect(self._toggle_api_visibility)
        form.addRow("API key:", self.api_key_edit)

        # Warning hint under the field. Setting ANTHROPIC_API_KEY in the
        # child env BYPASSES Claude Code's `/login` subscription auth —
        # if the user has a working subscription and types any non-empty
        # string here, `claude` rejects the override with "Invalid API
        # key". Surfacing the trade-off in-place is much friendlier
        # than letting the user discover it via a failed test run.
        api_key_hint = QLabel(
            "<span style='color:#a26000; font-size:9pt;'>"
            "If <code>claude</code> already works in your terminal "
            "(via <code>/login</code> subscription), leave this "
            "<b>empty</b>. Setting any value here overrides that auth "
            "and must be a valid <code>sk-ant-…</code> key from "
            "<code>console.anthropic.com</code>."
            "</span>")
        api_key_hint.setTextFormat(Qt.RichText)
        api_key_hint.setWordWrap(True)
        # Empty label cell + the hint widget in the value column, so
        # the warning lines up under the input instead of under the
        # "API key:" label.
        form.addRow("", api_key_hint)

        # Timeout: seconds, range 10–3600. Stored as ms.
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 3600)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setSingleStep(15)
        self.timeout_spin.setToolTip(
            "Hard timeout for one `claude -p` round-trip. After this the "
            "test dialog kills the subprocess and labels the run as "
            "timed-out.")
        form.addRow("Test timeout:", self.timeout_spin)

        layout.addLayout(form)
        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for btn in buttons.buttons():
            btn.setStyleSheet(BUTTON_STYLE)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_values(self) -> None:
        current_model = app_settings.get_model()
        # Match an existing combobox entry by data first; if no match,
        # treat as a free-form value and write it into the line edit.
        idx = self.model_combo.findData(current_model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        else:
            self.model_combo.setEditText(current_model)

        self.api_key_edit.setText(app_settings.get_api_key())
        self.timeout_spin.setValue(
            max(10, app_settings.get_test_timeout_ms() // 1000))

    def _toggle_api_visibility(self) -> None:
        """Flip echo mode and update the trailing icon. Two states only:
        Password ↔ Normal — same widget, no rebuild."""
        if self.api_key_edit.echoMode() == QLineEdit.Password:
            self.api_key_edit.setEchoMode(QLineEdit.Normal)
            self._reveal_action.setIcon(eye_slash_icon())
            self._reveal_action.setToolTip("Hide API key")
        else:
            self.api_key_edit.setEchoMode(QLineEdit.Password)
            self._reveal_action.setIcon(eye_icon())
            self._reveal_action.setToolTip("Show API key")

    def _on_accept(self) -> None:
        # Prefer the combobox's selected-data when an item is picked
        # by index; fall back to the typed text for free-form entries.
        idx = self.model_combo.currentIndex()
        if idx >= 0 and self.model_combo.itemData(idx) == self.model_combo.currentText():
            model_value = self.model_combo.itemData(idx) or ""
        else:
            model_value = self.model_combo.currentText().strip()
            # Strip the placeholder label if the user accidentally
            # selected it but didn't edit — store empty instead.
            if model_value.startswith("(default"):
                model_value = ""

        # Strip whitespace on save. A trailing newline (pasted from
        # email or a doc) or leading space would silently break a
        # valid sk-ant-… key, with the failure showing up only at
        # the next test run as an opaque "Invalid API key". Empty
        # input round-trips correctly because ``strip()`` on "" is "".
        app_settings.set_model(model_value)
        app_settings.set_api_key(self.api_key_edit.text().strip())
        app_settings.set_test_timeout_ms(self.timeout_spin.value() * 1000)
        self.accept()
