"""Chromium prerequisite checks and explicit installation for Job Atlas."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys


def chromium_status() -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
        check=False,
        capture_output=True,
        text=True,
    )
    match = re.search(r"Install location:\s+(.+)", result.stdout)
    location = Path(match.group(1).strip()).expanduser() if match else None
    return {
        "installed": bool(location and location.is_dir()),
        "install_location": str(location) if location else None,
        **(
            {"detail": result.stderr.strip() or "Unable to resolve Chromium location"}
            if result.returncode or location is None
            else {}
        ),
    }


def install_chromium() -> dict[str, object]:
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
    )
    result = chromium_status()
    if not result["installed"]:
        raise RuntimeError("Playwright completed but Chromium is still unavailable")
    return result
