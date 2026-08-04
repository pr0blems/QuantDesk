"""Best-effort Windows desktop notifications through fixed PowerShell scripts."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_TOAST_PS1 = _ASSETS_DIR / "toast.ps1"
_POPUP_PS1 = _ASSETS_DIR / "popup.ps1"
_POWERSHELL_CANDIDATES = (
    Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
    Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
)
_toast_enabled_cache: dict[str, bool | float | None] = {"v": None, "ts": 0.0}


def _find_shell() -> Path | None:
    """Select only a known absolute PowerShell executable path."""

    return next((path for path in _POWERSHELL_CANDIDATES if path.is_file()), None)


def _toast_system_enabled() -> bool:
    """Read the Windows toast preference and cache it for five minutes."""

    now = time.time()
    cached_at = float(_toast_enabled_cache["ts"] or 0)
    cached_value = _toast_enabled_cache["v"]
    if now - cached_at < 300 and isinstance(cached_value, bool):
        return cached_value

    enabled = True
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "ToastEnabled")
        enabled = bool(value)
    except (ImportError, OSError) as exc:
        # Notification delivery is non-critical, but the fallback must remain observable.
        _LOGGER.warning("unable to read Windows toast preference; using toast: %s", exc)

    _toast_enabled_cache.update(v=enabled, ts=now)
    return enabled


def windows_toast(title: str, body: str) -> None:
    """Show a toast or popup without allowing notification failure to stop trading."""

    if sys.platform != "win32":
        return
    shell = _find_shell()
    if shell is None:
        _LOGGER.warning("PowerShell notification executable is unavailable")
        return
    script = _TOAST_PS1 if _toast_system_enabled() else _POPUP_PS1
    if not script.is_file():
        _LOGGER.warning("notification script is unavailable: %s", script.name)
        return

    # The executable, script and switches come exclusively from fixed allowlists.
    # Title/body are passed as separate argv values, never interpreted by a shell.
    command = [
        str(shell),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Title",
        str(title),
        "-Body",
        str(body),
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable/script; shell is disabled
            command,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOGGER.warning("desktop notification failed: %s", exc)
        return
    if result.returncode:
        detail = (result.stderr or result.stdout or "no output").strip()[:200]
        _LOGGER.warning(
            "desktop notification script exited with code %s: %s",
            result.returncode,
            detail,
        )
