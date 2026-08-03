"""Windows 桌面提醒（双通道）：
- 系统通知开启 → Toast（assets/toast.ps1）
- 系统通知关闭（ToastEnabled=0）→ 自动降级为 WScript 弹窗（assets/popup.ps1）
失败一律静默降级，绝不影响主流程。"""
import os, shutil, subprocess, sys

_DIR = os.path.dirname(os.path.abspath(__file__))
_TOAST_PS1 = os.path.join(_DIR, "assets", "toast.ps1")
_POPUP_PS1 = os.path.join(_DIR, "assets", "popup.ps1")
_toast_enabled_cache = {"v": None, "ts": 0}

def _find_shell():
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        p = shutil.which(name)
        if p:
            return p
    cand = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    return cand if os.path.exists(cand) else None

def _toast_system_enabled():
    """读注册表缓存 5 分钟"""
    import time
    now = time.time()
    if now - _toast_enabled_cache["ts"] < 300:
        return _toast_enabled_cache["v"]
    v = True
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications")
        val, _ = winreg.QueryValueEx(k, "ToastEnabled")
        v = bool(val)
        winreg.CloseKey(k)
    except Exception:
        v = True  # 读不到就当开
    _toast_enabled_cache.update(v=v, ts=now)
    return v

def windows_toast(title, body):
    """统一入口：自动选择 Toast 或弹窗"""
    if sys.platform != "win32":
        return
    shell = _find_shell()
    if not shell:
        return
    ps1 = _TOAST_PS1 if _toast_system_enabled() else _POPUP_PS1
    if not os.path.exists(ps1):
        return
    try:
        subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", ps1, "-Title", title, "-Body", body],
            capture_output=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
