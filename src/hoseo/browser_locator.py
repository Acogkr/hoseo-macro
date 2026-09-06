"""Lightweight browser discovery with no Selenium dependency."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


def find_chrome() -> str | None:
    """Return a Chrome/Chromium executable path without importing Selenium."""
    for name in (
        "google-chrome", "google-chrome-stable", "chrome",
        "chromium", "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            return found

    system = platform.system()
    candidates: list[Path] = []
    if system == "Windows":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif system == "Darwin":
        candidates.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ])
    else:
        candidates.extend([
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/snap/bin/chromium"),
        ])

    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return None
