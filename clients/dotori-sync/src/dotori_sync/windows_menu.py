"""Windows Explorer right-click integration for dotori-sync.

Ports the context-menu approach from the original shelf-sync prototype this
tool succeeds: a per-user (``HKEY_CURRENT_USER``) registry entry under
``Directory\\shell`` and ``Directory\\Background\\shell``, so no administrator
rights are required and no separate installer or shell extension is needed.

Unlike shelf-sync's one-shot "sync now" menu entry, dotori-sync's menu starts
the persistent watcher (``dotori-sync watch``), so the console window it
opens stays open for as long as that folder is being watched; closing the
window (or Ctrl+C) stops it.
"""

from __future__ import annotations

import os
import sys

MENU_LABEL = "Sync with Dotori"
_BACKGROUND_KEY = r"Software\Classes\Directory\Background\shell\DotoriSync"
_DIRECTORY_KEY = r"Software\Classes\Directory\shell\DotoriSync"


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("The Explorer context menu is only available on Windows.")


def _resolve_launch_prefix() -> str:
    """Return the quoted command prefix that re-invokes dotori-sync.

    Normally this is the path to the installed ``dotori-sync(.exe)`` console
    script (``sys.argv[0]``). If dotori-sync was started as ``python -m
    dotori_sync`` instead, ``sys.argv[0]`` points at ``__main__.py``, which
    Explorer cannot execute directly, so fall back to re-invoking it the same
    way through the current interpreter.
    """
    exe_path = os.path.abspath(sys.argv[0])
    if exe_path.lower().endswith(".py"):
        return f'"{sys.executable}" -m dotori_sync'
    return f'"{exe_path}"'


def _build_command(placeholder: str) -> str:
    """Build the Explorer command line for one context-menu key.

    ``placeholder`` is ``%V`` (Background: the open folder) or ``%1``
    (Directory: the clicked folder); Explorer substitutes it with the actual
    path. ``|| pause`` only keeps the console open when the watcher exits
    immediately with an error (e.g. no sync credential configured yet) --
    normal operation runs until the window is closed.
    """
    prefix = _resolve_launch_prefix()
    return f'cmd.exe /c "{prefix} watch "{placeholder}" || pause"'


def install_context_menu() -> None:
    """Add "Sync with Dotori" to the Explorer right-click menu for folders."""
    _require_windows()
    import winreg

    for key_path, placeholder in ((_BACKGROUND_KEY, "%V"), (_DIRECTORY_KEY, "%1")):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValue(key, "", winreg.REG_SZ, MENU_LABEL)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\command") as key:
            winreg.SetValue(key, "", winreg.REG_SZ, _build_command(placeholder))


def uninstall_context_menu() -> None:
    """Remove the Explorer right-click menu entries, if present."""
    _require_windows()
    import winreg

    for key_path in (_BACKGROUND_KEY, _DIRECTORY_KEY):
        for sub_key in (key_path + r"\command", key_path):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub_key)
            except FileNotFoundError:
                pass
