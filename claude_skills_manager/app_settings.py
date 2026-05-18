"""Centralized accessors for user-configurable app settings.

Backed by ``QSettings`` under the same organization/app pair as the rest
of the UI state (project root, splitter sizes, etc.), but kept Qt-free
at the API surface so callers in domain modules can read settings
without pulling Qt into the import graph.

Three concerns live here:

* **Model** — which ``claude`` model to pass via ``--model`` when
  shelling out. Empty string means "let ``claude`` pick its default."
* **API key** — optional ``ANTHROPIC_API_KEY`` override. Empty means
  "inherit from environment / ``claude``'s own auth."
* **Test timeout** — hard ceiling on a single ``claude -p`` round-trip
  before the dialog kills the subprocess. Exposed in milliseconds to
  match the existing ``QTimer`` units everywhere it's consumed.

QSettings is constructed inside each accessor rather than cached at
module scope: it's cheap (microseconds), it picks up changes made by
parallel processes, and it dodges a Qt-app-required-at-import-time
constraint that a module-level instance would impose."""
from __future__ import annotations

from PySide6.QtCore import QSettings

_ORG = "ClaudeSkillsManager"
_APP = "ClaudeSkillsManager"

# Defaults are tuned for the common case: don't override the model
# (let ``claude`` pick), don't override the API key (inherit from
# ``claude`` auth), 3-minute timeout matches the previous hard-coded
# _TEST_RUN_TIMEOUT_MS in test_dialog.py so existing user expectations
# don't shift on upgrade.
DEFAULT_MODEL: str = ""
DEFAULT_API_KEY: str = ""
DEFAULT_TIMEOUT_MS: int = 3 * 60 * 1000


# Reasonable known models for the dropdown. Empty string is the
# "(default — let claude pick)" entry. Adding new models here surfaces
# them in the Settings dialog without code changes elsewhere.
KNOWN_MODELS: tuple[str, ...] = (
    "",  # default
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)


def _settings() -> QSettings:
    return QSettings(_ORG, _APP)


# ----------------------------------------------------------------- model
def get_model() -> str:
    return str(_settings().value("model", DEFAULT_MODEL) or "")


def set_model(value: str) -> None:
    _settings().setValue("model", value or "")


# ---------------------------------------------------------------- api key
def get_api_key() -> str:
    return str(_settings().value("api_key", DEFAULT_API_KEY) or "")


def set_api_key(value: str) -> None:
    _settings().setValue("api_key", value or "")


# ---------------------------------------------------------------- timeout
def get_test_timeout_ms() -> int:
    """Test-run hard timeout in milliseconds. Always returns a positive
    int; malformed stored values fall back to :data:`DEFAULT_TIMEOUT_MS`
    so a corrupted registry entry can't break the test runner."""
    raw = _settings().value("test_timeout_ms", DEFAULT_TIMEOUT_MS)
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    if ms < 1000:
        return DEFAULT_TIMEOUT_MS
    return ms


def set_test_timeout_ms(value: int) -> None:
    _settings().setValue("test_timeout_ms", int(value))
