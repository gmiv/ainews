#!/usr/bin/env python3
"""Self-bootstrapping launcher for ai_news_feed.

Launching this with *any* Python keeps your global / system site-packages
pristine: it creates an isolated virtualenv, installs the package (with the
``[all]`` extras) into it on first run, and re-execs the app inside that venv.
Subsequent launches are instant (the install is cached).

On WSL the venv is placed on the Linux filesystem (under ``~/.cache/ainews``)
rather than the project dir, because a venv on the Windows drive (``/mnt/...``)
is painfully slow to build and import. On native Linux/macOS it's a local
``.venv`` in the project.

    ./run                          # via the shell wrapper
    python ai_news_feed.py
    python ai_news_feed.py --setup         # build the venv, don't launch
    python ai_news_feed.py --venv-python   # print the venv's python path
    python ai_news_feed.py --clean         # remove the venv

Set AINEWS_NO_BOOTSTRAP=1 to run in the current interpreter (e.g. inside pipx/CI).
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _venv_dir():
    """Where the isolated venv lives (off the Windows drive on WSL)."""
    if str(ROOT).startswith("/mnt/"):
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache")
        tag = hashlib.sha1(str(ROOT).encode()).hexdigest()[:8]
        return Path(base) / "ainews" / f"venv-{tag}"
    return ROOT / ".venv"


VENV = _venv_dir()
VPY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
MARKER = VENV / ".ainews-installed"


def _in_our_venv():
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except Exception:
        return False


def _run_app():
    sys.path.insert(0, str(ROOT))
    from ainews.app import main
    main()


def _ensure_venv():
    """Create the venv and install the package into it (idempotent)."""
    if not VPY.exists():
        print(f"· ainews: creating isolated venv at {VENV} …", file=sys.stderr)
        VENV.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)

    pyproject = ROOT / "pyproject.toml"
    stale = (not MARKER.exists()) or (
        pyproject.exists() and MARKER.exists()
        and pyproject.stat().st_mtime > MARKER.stat().st_mtime
    )
    if stale:
        print("· ainews: installing dependencies (first run only) …",
              file=sys.stderr)
        subprocess.run([str(VPY), "-m", "pip", "install", "-q", "-U", "pip"],
                       check=True)
        subprocess.run([str(VPY), "-m", "pip", "install", "-q", "-e", ".[all]"],
                       cwd=str(ROOT), check=True)
        MARKER.write_text("ok\n")


def main():
    argv = sys.argv[1:]

    # Venv-management helpers (used by the Makefile; no bootstrap needed).
    if "--venv-python" in argv:
        print(str(VPY))
        return
    if "--clean" in argv:
        if VENV.exists():
            shutil.rmtree(VENV, ignore_errors=True)
            print(f"· ainews: removed {VENV}", file=sys.stderr)
        return

    setup_only = "--setup" in argv

    # Already isolated (our venv, or the user opted out) → run directly.
    if os.environ.get("AINEWS_NO_BOOTSTRAP") or _in_our_venv():
        if setup_only:
            print(f"· ainews: venv ready at {VENV}", file=sys.stderr)
            return
        _run_app()
        return

    # Bootstrap, then re-exec this script inside the venv.
    try:
        _ensure_venv()
        os.execv(str(VPY), [str(VPY), str(Path(__file__).resolve()), *argv])
    except Exception as exc:  # noqa: BLE001 - bootstrap must degrade, not crash
        print(f"· ainews: venv bootstrap skipped ({exc}); using the current "
              f"interpreter", file=sys.stderr)
        if setup_only:
            return
        _run_app()


if __name__ == "__main__":
    main()
