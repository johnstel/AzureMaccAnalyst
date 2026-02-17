"""Desktop launcher for the Azure MACC Analyst Streamlit app.

When packaged by PyInstaller the application files live inside
``sys._MEIPASS`` (--onedir) or a temp extraction folder (--onefile).
This launcher resolves the correct path to ``app.py`` regardless of
whether the app is running from source or from a frozen bundle.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _resolve_app_dir() -> Path:
    """Return the directory that contains app.py and src/."""
    if getattr(sys, "frozen", False):
        # Running inside a PyInstaller bundle
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def _open_browser_after_delay(url: str, delay: float = 3.5) -> None:
    """Wait for Streamlit to bind the port, then open the default browser."""
    time.sleep(delay)
    webbrowser.open(url)


def main() -> None:
    app_dir = _resolve_app_dir()
    app_path = app_dir / "app.py"

    # Point Streamlit at the correct directory so relative imports (src.*) work
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))

    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    os.environ.setdefault("STREAMLIT_SERVER_PORT", "8501")
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")
    os.environ.setdefault("STREAMLIT_SERVER_FILE_WATCHER_TYPE", "none")

    threading.Thread(
        target=_open_browser_after_delay,
        args=("http://localhost:8501",),
        daemon=True,
    ).start()

    from streamlit.web import cli as stcli

    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.headless", "true",
        "--browser.serverAddress", "localhost",
        "--server.fileWatcherType", "none",
        "--global.developmentMode", "false",
    ]
    stcli.main()


if __name__ == "__main__":
    main()
