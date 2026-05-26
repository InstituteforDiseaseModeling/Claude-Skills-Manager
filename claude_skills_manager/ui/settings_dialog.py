"""Modal settings dialog — model / API key / test timeout.

Reads and writes through :mod:`claude_skills_manager.app_settings` so
the dialog has no direct knowledge of the storage backend (QSettings
today, could be a JSON file or env vars later)."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QSpinBox, QVBoxLayout, QWidget,
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
        self.resize(520, 360)
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
            "<code>console.anthropic.com</code>.<br>"
            "Takes priority over <i>Environment ANTHROPIC_API_KEY</i> "
            "below when both are set."
            "</span>")
        api_key_hint.setTextFormat(Qt.RichText)
        api_key_hint.setWordWrap(True)
        # Empty label cell + the hint widget in the value column, so
        # the warning lines up under the input instead of under the
        # "API key:" label.
        form.addRow("", api_key_hint)

        # OS-level ANTHROPIC_API_KEY environment variable. Distinct from
        # the QSettings "API key" above — this field reads from / writes
        # to ``os.environ`` and (on Windows) persists via ``setx`` so
        # the value is visible to other tools (a fresh terminal, the
        # claude CLI invoked outside this app, the SDK in another
        # script). The QSettings API key takes priority when both are
        # populated; see app_settings.set_env_api_key.
        self.env_api_key_edit = QLineEdit()
        self.env_api_key_edit.setEchoMode(QLineEdit.Password)
        self.env_api_key_edit.setPlaceholderText(
            "(empty — ANTHROPIC_API_KEY is not set in your environment)")
        self.env_api_key_edit.setToolTip(
            "Reads / writes the OS-level ANTHROPIC_API_KEY environment "
            "variable. On Windows this persists via `setx`, so new "
            "shells / processes will see the value. Existing terminals "
            "keep their stale copy until restarted.")
        # Same reveal/hide convention as the API key field above —
        # one trailing action toggles between Password and Normal echo
        # modes, icon represents what clicking will DO (eye = show,
        # eye-slash = hide).
        self._env_reveal_action = self.env_api_key_edit.addAction(
            eye_icon(), QLineEdit.TrailingPosition)
        self._env_reveal_action.setToolTip("Show env value")
        self._env_reveal_action.triggered.connect(
            self._toggle_env_api_visibility)
        form.addRow("Environment\nANTHROPIC_API_KEY:", self.env_api_key_edit)

        # Hint under the env-key field. Clarifies the persistence
        # contract (per-OS) and the priority relationship with the
        # in-app field above. Color matches api_key_hint (subtle
        # caution amber) to read as informational rather than alarming.
        # Platform-specific wording: on Windows persistence is real
        # (setx writes to HKCU\Environment); on macOS/Linux this app
        # only updates the current process's env, since the persistence
        # target is shell-dependent and ambiguous.
        if sys.platform == "win32":
            env_hint_text = (
                "Persists to your user environment via "
                "<code>setx</code> and updates this app session "
                "immediately. For a <b>fresh app launch</b> to see "
                "the new value, <b>open a new terminal first</b> — "
                "the app inherits its environment from the shell "
                "that launched it, and existing terminals keep their "
                "stale value until restarted. Clear the field to "
                "remove the variable entirely.")
        else:
            env_hint_text = (
                "<b>This app session only</b> on macOS / Linux — "
                "persisting to a shell startup file isn't done here "
                "(too many candidates: <code>~/.bashrc</code>, "
                "<code>~/.zshrc</code>, <code>~/.profile</code>, …). "
                "Add an <code>export ANTHROPIC_API_KEY=…</code> line "
                "to your shell rc by hand for permanence.")
        env_hint = QLabel(
            f"<span style='color:#a26000; font-size:9pt;'>"
            f"{env_hint_text}"
            f"</span>")
        env_hint.setTextFormat(Qt.RichText)
        env_hint.setWordWrap(True)
        form.addRow("", env_hint)

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
        # Snapshot the env value at open time so we can detect a real
        # change in ``_on_accept`` and only shell out to setx when the
        # user actually edited the field. Avoids spurious console
        # flashes / registry writes on a no-op OK.
        self._env_api_key_initial = app_settings.get_env_api_key()
        self.env_api_key_edit.setText(self._env_api_key_initial)
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

    def _toggle_env_api_visibility(self) -> None:
        """Flip echo mode for the env field. Independent of the in-app
        API-key visibility toggle on purpose — the two fields can each
        be revealed without affecting the other (the user may want to
        compare without exposing the more-sensitive in-app override)."""
        if self.env_api_key_edit.echoMode() == QLineEdit.Password:
            self.env_api_key_edit.setEchoMode(QLineEdit.Normal)
            self._env_reveal_action.setIcon(eye_slash_icon())
            self._env_reveal_action.setToolTip("Hide env value")
        else:
            self.env_api_key_edit.setEchoMode(QLineEdit.Password)
            self._env_reveal_action.setIcon(eye_icon())
            self._env_reveal_action.setToolTip("Show env value")

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

        # Only touch the OS env when the user actually changed the
        # field. Shelling out to setx / reg on every OK click would
        # be a waste of cycles and (more importantly) would flash a
        # console window on non-pythonw launches even for a no-op
        # save. Snapshot is captured in ``_load_values``.
        new_env_value = self.env_api_key_edit.text().strip()
        if new_env_value != self._env_api_key_initial:
            ok, msg = app_settings.set_env_api_key(new_env_value)
            if sys.platform != "win32":
                # macOS / Linux: set_env_api_key only updated this
                # process's os.environ. The user explicitly asked us
                # to be transparent about this rather than silently
                # accept (Settings dialog clicking OK shouldn't lie
                # about what just happened). Don't echo the value in
                # the message body — shoulder-surfing risk.
                QMessageBox.information(
                    self,
                    "Environment update — this session only",
                    "ANTHROPIC_API_KEY was updated for this app "
                    "session, but persistence to your user "
                    "environment isn't supported here.\n\n"
                    "Effect: the current app will use the new value "
                    "for ``claude`` runs. Other terminals, future "
                    "app launches, and tools that read your shell "
                    "environment at start-up will NOT see it.\n\n"
                    "To persist, add an `export ANTHROPIC_API_KEY=…` "
                    "line to your shell's startup file "
                    "(e.g. ~/.bashrc or ~/.zshrc) by hand.")
            elif not ok:
                # Windows persistence failed. The current-process
                # os.environ has already been updated, so don't
                # refuse the dialog — surface a warning and accept
                # anyway. The user can close + reopen Settings to
                # see whether the env-level value reflects what
                # they expect.
                QMessageBox.warning(
                    self, "Environment update partially failed",
                    "ANTHROPIC_API_KEY was updated for this app session, "
                    "but persisting it to the user environment failed:\n\n"
                    f"{msg}\n\n"
                    "Other tools / new shells will not see the new value "
                    "until the underlying issue is fixed.")
        self.accept()
