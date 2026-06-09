"""Open a URL in the user's real browser — robustly, including under WSL.

Python's ``webbrowser`` often fails silently on WSL (no registered browser), so
we prefer Windows-side openers (``wslview`` / ``explorer.exe`` / PowerShell /
cmd) when running under WSL, falling back to the usual Linux/macOS openers and
finally to ``webbrowser``. Best-effort: returns True if an opener was launched.
"""
import shutil
import subprocess
import webbrowser


def _is_wsl():
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def _candidates(url):
    """Ordered list of opener argv lists to try for ``url``."""
    cmds = []
    if _is_wsl():
        # explorer.exe handles URLs directly (no cmd metachar parsing); PowerShell
        # Start-Process and `cmd /c start` are sturdy fallbacks.
        if shutil.which("wslview"):
            cmds.append(["wslview", url])
        if shutil.which("explorer.exe"):
            cmds.append(["explorer.exe", url])
        if shutil.which("powershell.exe"):
            cmds.append(["powershell.exe", "-NoProfile", "-Command",
                         "Start-Process", url])
        if shutil.which("cmd.exe"):
            cmds.append(["cmd.exe", "/c", "start", "", url])
    for exe in ("xdg-open", "open", "wslview", "sensible-browser",
                "x-www-browser"):
        if shutil.which(exe):
            cmds.append([exe, url])
    return cmds


def open_url(url):
    """Launch the default browser for ``url``; return True if an opener started."""
    if not url:
        return False
    for cmd in _candidates(url):
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            return True
        except Exception:
            continue
    # Last resort: Python's own browser resolution.
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False
