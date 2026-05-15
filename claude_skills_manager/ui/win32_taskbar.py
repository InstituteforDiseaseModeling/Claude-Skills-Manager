"""Windows-specific taskbar icon binding via per-window IPropertyStore.

Why this module exists
----------------------
Setting ``QApplication.setWindowIcon()`` and even
``QMainWindow.setWindowIcon()`` is **not enough** to make the Windows
taskbar show a custom icon for a run-from-source Python GUI app. The
taskbar resolves icons in this order:

1. Look up the window's AppUserModelID (per-window, falling back to
   per-process via ``SetCurrentProcessExplicitAppUserModelID``).
2. Look up the icon registered for that AppUserModelID — typically
   set by an installer in the registry / Start Menu shortcut.
3. If no registered icon, show a blank/generic icon.

For installed apps step 2 succeeds. For run-from-source apps, no
installer ran, so step 2 fails and the taskbar shows blank — even
though ``WM_SETICON`` *did* attach an icon to the window (which the
title bar and Alt+Tab use just fine).

The fix is to attach the icon resource to the window itself via the
``IPropertyStore`` returned by ``SHGetPropertyStoreForWindow``,
setting two keys:

* ``PKEY_AppUserModel_ID`` — the AppUserModelID for this window.
* ``PKEY_AppUserModel_RelaunchIconResource`` — a path to an .ico
  file (with optional ``,N`` index), telling Windows where to read
  the icon for this window's taskbar entry.

This is the same mechanism the Windows shell uses internally for
installed apps; we're just doing it at runtime instead of install-time.

Pure-ctypes implementation (no comtypes / pywin32 dependency) so the
module slots into the project's "minimal dependencies" stance from
DESIGN.md §1. The COM plumbing is the standard
QueryInterface / vtable / Release dance.
"""
from __future__ import annotations

import sys
from pathlib import Path


def apply_window_appusermodel(
    hwnd: int, app_id: str, ico_path: Path | str,
) -> bool:
    """Bind ``app_id`` and ``ico_path`` to the given top-level window via
    Windows' ``IPropertyStore`` interface, so the taskbar uses our icon
    for that window's taskbar entry.

    Returns True on success, False on any failure (non-Windows, missing
    DLL exports, COM error, file path doesn't resolve). Failures are
    silent and non-fatal — the in-app surfaces (title bar, toolbar,
    Alt+Tab) still get the icon via the ``setWindowIcon`` paths."""
    if sys.platform != "win32":
        return False
    try:
        return _apply(hwnd, app_id, str(ico_path))
    except (OSError, AttributeError, ValueError):
        # OSError: COM HRESULT failure surfaced by ctypes.
        # AttributeError: missing DLL export on a stripped Windows.
        # ValueError: GUID parse failure.
        return False


# ---------------------------------------------------------------------------
# Implementation — gated behind sys.platform check so the import stays
# safe on non-Windows. ctypes.windll itself only exists on Windows.
# ---------------------------------------------------------------------------
def _apply(hwnd: int, app_id: str, ico_path: str) -> bool:
    import ctypes
    from ctypes import wintypes

    # ---- COM types --------------------------------------------------------
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def _guid(s: str) -> GUID:
        # CLSIDFromString accepts "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}".
        g = GUID()
        ole32 = ctypes.windll.ole32
        hr = ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
        if hr != 0:
            raise ValueError(f"Invalid GUID: {s!r} (HRESULT {hr:#010x})")
        return g

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    # PROPVARIANT is a 16-byte (24 on x64) tagged union. We only need
    # VT_LPWSTR — variant tag 31, with a wide-string pointer at offset 8.
    # Lay out the full struct size so SetValue() doesn't read past our
    # buffer; field padding makes the union work for our one case.
    class PROPVARIANT(ctypes.Structure):
        _fields_ = [
            ("vt",         wintypes.USHORT),
            ("wReserved1", wintypes.WORD),
            ("wReserved2", wintypes.WORD),
            ("wReserved3", wintypes.WORD),
            ("pwszVal",    wintypes.LPWSTR),
            # Pad to the full PROPVARIANT size (16 on x86, 24 on x64).
            ("_padding",   ctypes.c_byte * (16 if ctypes.sizeof(ctypes.c_void_p) == 4 else 16)),
        ]

    VT_LPWSTR = 31

    # ---- Property keys ----------------------------------------------------
    # PKEY_AppUserModel_ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, pid=5
    PKEY_APP_ID = PROPERTYKEY()
    PKEY_APP_ID.fmtid = _guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}")
    PKEY_APP_ID.pid = 5

    # PKEY_AppUserModel_RelaunchIconResource =
    #   {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, pid=3
    PKEY_RELAUNCH_ICON = PROPERTYKEY()
    PKEY_RELAUNCH_ICON.fmtid = _guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}")
    PKEY_RELAUNCH_ICON.pid = 3

    # IID_IPropertyStore = {886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}
    IID_IPROPSTORE = _guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")

    shell32 = ctypes.windll.shell32
    ole32   = ctypes.windll.ole32

    # SHGetPropertyStoreForWindow(HWND, REFIID, PVOID*) -> HRESULT
    shell32.SHGetPropertyStoreForWindow.argtypes = [
        wintypes.HWND, ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.HRESULT

    # CoInitialize for the calling thread — Qt's main thread may already
    # have COM initialized via Qt itself; CoInitialize tolerates being
    # called twice (returns S_FALSE) so we don't need to check first.
    ole32.CoInitialize(None)

    prop_store = ctypes.c_void_p()
    hr = shell32.SHGetPropertyStoreForWindow(
        hwnd, ctypes.byref(IID_IPROPSTORE), ctypes.byref(prop_store))
    if hr != 0 or not prop_store:
        return False

    # ---- IPropertyStore vtable --------------------------------------------
    # IPropertyStore : IUnknown
    #   IUnknown:    QueryInterface(0), AddRef(1), Release(2)
    #   IPropertyStore: GetCount(3), GetAt(4), GetValue(5),
    #                   SetValue(6), Commit(7)
    vtbl_ptr = ctypes.cast(prop_store,
                           ctypes.POINTER(ctypes.c_void_p))[0]
    vtbl = ctypes.cast(vtbl_ptr,
                       ctypes.POINTER(ctypes.c_void_p * 8))[0]

    SetValueProto = ctypes.WINFUNCTYPE(
        ctypes.HRESULT, ctypes.c_void_p,
        ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    CommitProto  = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)
    ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

    SetValue = SetValueProto(vtbl[6])
    Commit   = CommitProto(vtbl[7])
    Release  = ReleaseProto(vtbl[2])

    try:
        # ---- Build PROPVARIANT for AppUserModelID -------------------------
        pv_id = PROPVARIANT()
        pv_id.vt = VT_LPWSTR
        # Keep the LPWSTR alive until SetValue+Commit return.
        id_buf = ctypes.create_unicode_buffer(app_id)
        pv_id.pwszVal = ctypes.cast(id_buf, wintypes.LPWSTR)
        if SetValue(prop_store,
                    ctypes.byref(PKEY_APP_ID),
                    ctypes.byref(pv_id)) != 0:
            return False

        # ---- Build PROPVARIANT for RelaunchIconResource -------------------
        # Format: "<absolute-path-to-.ico>,<resource-index>". For a plain
        # .ico file the index is 0 (the file itself contains the image).
        icon_resource = f"{ico_path},0"
        pv_icon = PROPVARIANT()
        pv_icon.vt = VT_LPWSTR
        icon_buf = ctypes.create_unicode_buffer(icon_resource)
        pv_icon.pwszVal = ctypes.cast(icon_buf, wintypes.LPWSTR)
        if SetValue(prop_store,
                    ctypes.byref(PKEY_RELAUNCH_ICON),
                    ctypes.byref(pv_icon)) != 0:
            return False

        # Commit makes the changes visible to the shell.
        if Commit(prop_store) != 0:
            return False
        return True
    finally:
        Release(prop_store)
