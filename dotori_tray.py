#!/usr/bin/env python3
"""Windows system tray companion for install.py.

Polls `install.py --status --json-output` and shows the result as a tray
icon, with a right-click menu that maps directly onto install.py's existing
operate flags. No server-control logic lives here -- this only wraps what
`install.py --run/--stop/--restart/--status` already do, and makes sure the
Docker daemon is reachable (launching Docker Desktop itself if needed)
before polling status.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent
APP_URL = "http://127.0.0.1:8000/"
POLL_INTERVAL_SECONDS = 10.0
DOCKER_DESKTOP_PATHS = (
    r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def run_install(
    args: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    timeout: float | None = 60,
) -> "subprocess.CompletedProcess[str]":
    """Invoke install.py with the given CLI args and return the completed process."""
    return runner(
        [sys.executable, str(PROJECT_ROOT / "install.py"), *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def fetch_status(*, scope: str = "production", runner: Runner = subprocess.run) -> dict | None:
    """Run install.py --status --json-output and parse the result.

    Returns None if install.py failed to run or produced non-JSON output
    (e.g. Docker isn't reachable yet) -- callers treat that as "unknown".
    """
    try:
        result = run_install(["--status", "--json-output", "--scope", scope], runner=runner)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def summarize_status(report: dict | None) -> tuple[str, str]:
    """Classify a status report into (state, tooltip); state is one of
    "running", "degraded", "stopped", "unknown"."""
    if report is None:
        return "unknown", "Dotori: status unavailable"

    services = (report.get("connection") or {}).get("docker_services") or {}
    if not services:
        return "unknown", "Dotori: no services found"

    states = {name: info.get("state") for name, info in services.items()}
    running = [name for name, state in states.items() if state == "running"]
    if len(running) == len(states):
        return "running", f"Dotori: running ({len(running)} services)"
    if not running:
        return "stopped", "Dotori: stopped"
    not_running = sorted(set(states) - set(running))
    return "degraded", f"Dotori: degraded ({', '.join(not_running)} not running)"


def is_docker_daemon_up(*, runner: Runner = subprocess.run) -> bool:
    try:
        result = runner(["docker", "info"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ensure_docker_desktop_running(
    *,
    runner: Runner = subprocess.run,
    launcher: Callable[[str], None] | None = None,
    docker_desktop_paths: Sequence[str] = DOCKER_DESKTOP_PATHS,
    path_exists: Callable[[str], bool] = os.path.exists,
    poll_interval: float = 2.0,
    timeout: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """Make sure the Docker daemon is reachable, launching Docker Desktop if needed.

    Returns True once `docker info` succeeds, False if it never comes up
    within `timeout` seconds or Docker Desktop isn't installed at a known
    path. Doesn't rely on Docker Desktop's own "start at login" setting --
    if the daemon isn't up yet, this launches the app directly.
    """
    if is_docker_daemon_up(runner=runner):
        return True

    exe_path = next((p for p in docker_desktop_paths if path_exists(p)), None)
    if exe_path is None:
        return False

    if launcher is None:
        launcher = lambda path: subprocess.Popen([path])  # noqa: E731
    launcher(exe_path)

    deadline = now() + timeout
    while now() < deadline:
        sleep(poll_interval)
        if is_docker_daemon_up(runner=runner):
            return True
    return False


_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_VALUE_NAME = "DotoriTray"


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Autostart registration is only available on Windows.")


def register_autostart() -> None:
    """Start this tray app at login, via a per-user registry Run key.

    Uses HKEY_CURRENT_USER (like windows_menu.py's context menu), so no
    administrator rights are needed. This is a deliberate, explicit action
    (call it yourself, or wire a setup-wizard checkbox to it) -- it is never
    registered automatically on behalf of the user.
    """
    _require_windows()
    import winreg

    command = f'"{sys.executable}" "{Path(__file__).resolve()}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as key:
        winreg.SetValueEx(key, _AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, command)


def unregister_autostart() -> None:
    """Remove the autostart registry entry, if present."""
    _require_windows()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _AUTOSTART_VALUE_NAME)
    except FileNotFoundError:
        pass


ICON_FILE = PROJECT_ROOT / "dotori.ico"


def build_icon_image():
    """Load the Dotori acorn mark (see scripts/generate_dotori_ico.py) for
    the tray icon. Status is conveyed through the tooltip text, not the
    icon image, so the mark itself never needs recoloring."""
    from PIL import Image

    with Image.open(ICON_FILE) as source:
        source.load()
        return source.convert("RGBA").resize((64, 64), Image.LANCZOS)


def _open_dotori(icon=None, item=None):
    webbrowser.open(APP_URL)


def _make_action(*args: str):
    def _action(icon=None, item=None):
        run_install(list(args))

    return _action


def build_menu():
    import pystray

    return pystray.Menu(
        pystray.MenuItem("Open Dotori", _open_dotori, default=True),
        pystray.MenuItem("Start", _make_action("--run")),
        pystray.MenuItem("Restart", _make_action("--restart")),
        pystray.MenuItem("Stop", _make_action("--stop")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit tray (keeps server running)", lambda icon, item: icon.stop()),
    )


def _poll_loop(icon, stop_event: threading.Event, interval: float = POLL_INTERVAL_SECONDS) -> None:
    while not stop_event.is_set():
        _state, tooltip = summarize_status(fetch_status())
        icon.title = tooltip
        stop_event.wait(interval)


def main() -> int:
    try:
        import pystray
    except ImportError:
        print(
            "The tray app needs the 'pystray' and 'pillow' packages.\n"
            "Install them with: pip install pystray pillow",
            file=sys.stderr,
        )
        return 1

    ensure_docker_desktop_running()

    stop_event = threading.Event()
    icon = pystray.Icon("dotori", build_icon_image(), "Dotori", build_menu())
    poll_thread = threading.Thread(target=_poll_loop, args=(icon, stop_event), daemon=True)
    poll_thread.start()
    try:
        icon.run()
    finally:
        stop_event.set()
    return 0


if __name__ == "__main__":
    if "--register-autostart" in sys.argv[1:]:
        register_autostart()
        print("Registered dotori_tray.py to start at login.")
        raise SystemExit(0)
    if "--unregister-autostart" in sys.argv[1:]:
        unregister_autostart()
        print("Removed dotori_tray.py from login autostart.")
        raise SystemExit(0)
    raise SystemExit(main())
