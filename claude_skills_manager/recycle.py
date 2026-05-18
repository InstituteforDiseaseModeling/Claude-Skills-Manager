"""Soft-delete a filesystem path by moving it to the OS Recycle Bin.

Qt-free by design — same seam pattern as :mod:`claude_trust`. The GUI
calls this after the user double-confirms a "Delete Skill" action; this
module only touches the filesystem via the OS shell API.

On Windows (the project's primary target), this uses
``SHFileOperationW`` from ``shell32`` with the ``FOF_ALLOWUNDO`` flag
— the same call ``send2trash`` and File Explorer's "Delete" key
ultimately make. Result: the folder lands in the user's Recycle Bin
and can be restored from there until they empty it. No new third-party
dependency required.

On non-Windows platforms this raises :class:`NotImplementedError` —
the GUI is Windows-primary, and silently falling back to ``shutil.rmtree``
would be the opposite of "soft delete". A future macOS/Linux port can
add platform branches here without touching the call site."""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path


# Win32 SHFileOperationW constants — values from MSRT's shellapi.h.
# Defined locally rather than imported because we don't ship a
# pywin32 dependency; the integer values are stable API.
_FO_DELETE         = 0x0003
_FOF_ALLOWUNDO     = 0x0040   # The whole point — route through Recycle Bin.
_FOF_NOCONFIRMATION = 0x0010  # Suppress shell's own "are you sure?" prompt;
                              # the GUI does its own (double) confirmation.
_FOF_NOERRORUI     = 0x0400   # Suppress shell error dialogs; we surface
                              # failure via the caller's QMessageBox instead.
_FOF_SILENT        = 0x0004   # Suppress the shell's progress UI for what
                              # is, in this app, a small folder.


class _SHFILEOPSTRUCTW(ctypes.Structure):
    """Mirror of the Win32 ``SHFILEOPSTRUCTW`` C struct.

    Field order, types, and packing must match shellapi.h exactly —
    SHFileOperationW does a memcpy-style read from the pointer, so
    a wrong layout silently corrupts the call. ``fFlags`` is
    ``FILEOP_FLAGS`` which is a ``WORD`` (16-bit) — narrower than a
    typical UINT. Getting that wrong is the most common ctypes
    transcription mistake here."""
    _fields_ = [
        ("hwnd",                   wintypes.HWND),
        ("wFunc",                  wintypes.UINT),
        ("pFrom",                  wintypes.LPCWSTR),
        ("pTo",                    wintypes.LPCWSTR),
        ("fFlags",                 ctypes.c_ushort),  # FILEOP_FLAGS = WORD
        ("fAnyOperationsAborted",  wintypes.BOOL),
        ("hNameMappings",          wintypes.LPVOID),
        ("lpszProgressTitle",      wintypes.LPCWSTR),
    ]


def send_to_recycle_bin(path: Path) -> None:
    """Move ``path`` (a file or a directory) to the OS Recycle Bin.

    Returns ``None`` on success. Raises :class:`OSError` on any failure
    from the underlying shell call, :class:`FileNotFoundError` if the
    path does not exist, :class:`NotImplementedError` on non-Windows
    platforms.

    The shell API silently no-ops on a missing path — we check
    explicitly because "did nothing, returned 0" is indistinguishable
    from "succeeded" and the caller deserves a real error in that case."""
    if sys.platform != "win32":
        raise NotImplementedError(
            "send_to_recycle_bin is only implemented on Windows. "
            "On macOS/Linux, add a platform branch here or shell out "
            "to `trash`/`gio trash`.")

    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Cannot recycle non-existent path: {resolved}")

    # pFrom must be **double-null-terminated** — it's documented as
    # a list-of-strings, each null-terminated, with an extra null at
    # the end to mark end-of-list. A single trailing ``\x00`` would
    # cause SHFileOperationW to read past the buffer for a second
    # path that never comes, returning an opaque non-zero result.
    pFrom = f"{resolved}\x00\x00"

    op = _SHFILEOPSTRUCTW()
    op.hwnd = 0
    op.wFunc = _FO_DELETE
    op.pFrom = pFrom
    op.pTo = None
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION
                 | _FOF_NOERRORUI | _FOF_SILENT)
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    # Documented contract: 0 = success, non-zero = error code (NOT a
    # Win32 error — a shell-specific code, see MSDN's
    # "SHFileOperation return values" table). We surface it as-is in
    # the OSError so a debug session has the actual number to look up.
    if result != 0:
        raise OSError(
            f"SHFileOperationW returned {result} (0x{result:x}) "
            f"for {resolved}")
    if op.fAnyOperationsAborted:
        raise OSError(
            f"Recycle of {resolved} was aborted (user cancel or "
            "permission denial inside the shell layer).")
