"""Centralized accessors for user-configurable app settings.

Backed by ``QSettings`` under the same organization/app pair as the rest
of the UI state (project root, splitter sizes, etc.), but kept Qt-free
at the API surface so callers in domain modules can read settings
without pulling Qt into the import graph.

Four concerns live here:

* **Model** — which ``claude`` model to pass via ``--model`` when
  shelling out. Empty string means "let ``claude`` pick its default."
* **API key** — optional ``ANTHROPIC_API_KEY`` override. Empty means
  "inherit from environment / ``claude``'s own auth." This value is
  stored in QSettings and takes priority over the OS-level env var
  when both are set (see :func:`get_env_api_key` and the priority
  comment on :func:`set_env_api_key`).
* **Environment ANTHROPIC_API_KEY** — read/write of the user's OS-level
  ``ANTHROPIC_API_KEY`` environment variable. Distinct from the
  QSettings ``API key`` above: this is the value other tools (a fresh
  terminal, the ``claude`` CLI invoked outside this app, the Anthropic
  SDK in another script) will see. The in-app API key wins when both
  are populated.
* **Test timeout** — hard ceiling on a single ``claude -p`` round-trip
  before the dialog kills the subprocess. Exposed in milliseconds to
  match the existing ``QTimer`` units everywhere it's consumed.

QSettings is constructed inside each accessor rather than cached at
module scope: it's cheap (microseconds), it picks up changes made by
parallel processes, and it dodges a Qt-app-required-at-import-time
constraint that a module-level instance would impose."""
from __future__ import annotations

import os
import subprocess
import sys

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


# ------------------------------------- Environment ANTHROPIC_API_KEY (OS-level)
# Distinct from the QSettings-backed ``API key`` above: this reads /
# writes the user's OS-level ANTHROPIC_API_KEY environment variable
# directly, so any other tool that consults that variable (a fresh
# terminal, the ``claude`` CLI invoked outside this app, another SDK
# script) sees the same value.
#
# Priority when both are set: the QSettings ``API key`` wins. The test
# runner builds the child env as ``{**os.environ, **env_overrides}``
# (see ``test_dialog._worker_main``) and ``env_overrides`` is populated
# from ``get_api_key()`` via ``claude_env_overrides``. The spread order
# means a non-empty in-app API key overrides whatever is in os.environ
# — including the very value this section reads / writes.
ANTHROPIC_API_KEY_ENV: str = "ANTHROPIC_API_KEY"


def get_env_api_key() -> str:
    """Read ``ANTHROPIC_API_KEY`` from the current process's
    environment. Returns empty string when unset."""
    return os.environ.get(ANTHROPIC_API_KEY_ENV, "") or ""


def set_env_api_key(value: str) -> tuple[bool, str]:
    """Persist ``ANTHROPIC_API_KEY`` to the user's OS-level environment
    and update the current process's ``os.environ`` immediately.

    On Windows: shells out to ``setx`` for non-empty values (which
    writes to ``HKCU\\Environment``) or ``reg delete`` to fully remove
    the variable when the user clears the field. Setting to an empty
    string would leave a literal empty-string variable behind, which
    is observably different from "unset" — third-party tools that
    check ``"ANTHROPIC_API_KEY" in os.environ`` would still see it.
    ``reg delete`` is the only correct way to truly clear it.

    The newly set value is visible to processes launched after the
    call. Existing terminals / IDEs keep their stale snapshot until
    restarted — there's no way around that on Windows; the user
    environment block is only read at process start.

    On non-Windows: only ``os.environ`` for this process is updated.
    Persisting to a shell rc file is out of scope (and ambiguous —
    bash vs zsh vs fish, login vs interactive). The non-Windows code
    path keeps the in-app GUI consistent (open Settings, see / change
    the value for this run) without claiming to do something it
    doesn't.

    Returns ``(ok, message)``. ``ok=False`` carries a human-readable
    failure description suitable for surfacing in a QMessageBox; the
    UI layer is expected to keep going (the in-process ``os.environ``
    has still been updated, so the *current* app session sees the
    change even if persistence failed)."""
    value = value or ""

    # Current-process update is the safe, always-applicable half — do
    # it first so even if persistence below fails the user sees the
    # change in this session's behavior.
    if value:
        os.environ[ANTHROPIC_API_KEY_ENV] = value
    else:
        os.environ.pop(ANTHROPIC_API_KEY_ENV, None)

    if sys.platform != "win32":
        # Mirrors the recycle.py / win32_taskbar.py gating pattern:
        # Windows-only persistence, graceful no-op elsewhere.
        return True, ""

    # CREATE_NO_WINDOW (0x08000000) suppresses the brief console
    # flash that setx / reg.exe would otherwise show when the app is
    # launched via pythonw / a non-console entry point. Subprocess
    # output is still captured for the failure-message path.
    no_window = 0x08000000
    try:
        if value:
            result = subprocess.run(
                ["setx", ANTHROPIC_API_KEY_ENV, value],
                capture_output=True, text=True, timeout=10,
                creationflags=no_window,
            )
        else:
            # reg delete returns exit 1 if the value didn't exist —
            # we treat that as success because the *outcome* (no env
            # var) matches the request. Distinguished from a real
            # failure by checking the stderr text for "unable" /
            # "system was unable" wording. Cheap heuristic; the
            # success-path is the common case.
            result = subprocess.run(
                ["reg", "delete", "HKCU\\Environment",
                 "/v", ANTHROPIC_API_KEY_ENV, "/f"],
                capture_output=True, text=True, timeout=10,
                creationflags=no_window,
            )
            if result.returncode != 0:
                stderr_lc = (result.stderr or "").lower()
                if "unable to find" in stderr_lc or \
                        "cannot find" in stderr_lc:
                    return True, ""
        if result.returncode != 0:
            msg = (result.stderr or result.stdout
                   or "command exited non-zero").strip()
            return False, msg
        return True, ""
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


# --------------------------------------------------- Test-Skill session ids
# §7.68: there is no cross-open persistence for Test Skill conversation
# ids. Earlier iterations (§7.65 introduced QSettings persistence, §7.67
# refined it to per-(skill, cwd) keying) made reopening the dialog
# resume the previous conversation. User feedback was that "open dialog
# = fresh session" is the right semantic, so the persistence layer was
# removed entirely and the session id now lives only on the dialog
# instance. Closing the window drops it; opening another always starts
# clean. The dialog still preserves multi-turn context within one
# open window — capture-and-resume happens in-memory across consecutive
# Runs in the same instance.
#
# Old keys under ``test_dialog/sessions/...`` are left in QSettings as
# orphaned values for users upgrading across §7.65 → §7.68 — no code
# reads them, so they're inert. A registry cleanup pass isn't worth
# its own one-shot migration for the bug fix.
