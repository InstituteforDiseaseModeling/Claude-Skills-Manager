"""Single-call file logger configuration for the app.

Behavior:

* One log file, ``claude_skills_manager.log``, next to ``main.py``.
* **Truncated on every launch** (``mode="w"``) — fresh slate each run
  so reproducing a bug means "launch app, repro, send the log,"
  with no chronological merge required.
* Root logger is configured at INFO by default; bumped to DEBUG via
  the ``CLAUDE_SKILLS_DEBUG`` env var without code changes.
* The Qt message handler installed in ``main.py`` and the existing
  ``_log()`` print calls in dialogs both route here — one destination,
  one timeline.

Qt-free so it can be invoked before ``QApplication`` exists (and so a
future CLI / headless variant could share it)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


# Resolve the "app directory" as the folder containing the entry-point
# script. ``sys.argv[0]`` is the script path when run via
# ``python main.py``; falling back to the package's own location keeps
# the log near the code rather than landing in CWD when launched from
# elsewhere (PowerShell, scheduled task, IDE).
def _app_dir() -> Path:
    if sys.argv and sys.argv[0]:
        candidate = Path(sys.argv[0]).resolve().parent
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent


_LOG_FILENAME = "claude_skills_manager.log"


def log_file_path() -> Path:
    """Absolute path of the current log file. Computed each call so a
    test setting ``sys.argv[0]`` at runtime is reflected. Cheap."""
    return _app_dir() / _LOG_FILENAME


_configured: bool = False


def configure_logging() -> Path:
    """Install the file handler on the root logger. Idempotent —
    subsequent calls return the same path and do not double-install.

    Returns the log file path so the caller (typically ``main.py``)
    can surface it in diagnostics on startup."""
    global _configured
    path = log_file_path()
    if _configured:
        return path

    # Root logger; we want every named logger in the app to inherit
    # this handler so caller modules can ``logging.getLogger(__name__)``
    # without their own setup.
    root = logging.getLogger()
    level_name = os.environ.get("CLAUDE_SKILLS_DEBUG", "").strip().lower()
    root.setLevel(logging.DEBUG if level_name in ("1", "true", "yes", "on")
                  else logging.INFO)

    # Drop any pre-existing handlers — a re-import / reload during
    # development can otherwise leave stale handlers attached and
    # produce duplicate lines.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            path, mode="w", encoding="utf-8", delay=False)
    except OSError as e:
        # If the log file can't be opened (read-only filesystem,
        # locked by another process, etc.), fall back to stderr so
        # diagnostics aren't lost entirely. The app keeps running.
        sys.stderr.write(
            f"[logging] Could not open log file {path}: {e}\n"
            f"          Falling back to stderr logging.\n")
        file_handler = logging.StreamHandler(sys.stderr)

    formatter = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _configured = True
    logging.getLogger("claude_skills_manager").info(
        "Log file opened: %s", path)
    return path


def log_qt_message(category: str, message: str, severity: str = "warning") -> None:
    """Bridge from Qt's installed message handler to standard logging.

    Severity is mapped from Qt's ``QtMsgType`` names ("info", "warning",
    "critical", "fatal", "debug") to the corresponding ``logging``
    levels. Unknown severities default to WARNING so nothing is lost."""
    logger = logging.getLogger(f"qt.{category}" if category else "qt")
    level = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "critical": logging.ERROR,
        "fatal": logging.CRITICAL,
    }.get(severity.lower(), logging.WARNING)
    logger.log(level, "%s", message)
