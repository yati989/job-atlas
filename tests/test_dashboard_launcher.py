from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.dashboard import launcher


def test_public_dashboard_launcher_pins_ipv4_loopback(monkeypatch):
    captured: list[str] = []

    def fake_streamlit_main():
        captured.extend(sys.argv)
        return 0

    monkeypatch.setattr("streamlit.web.cli.main", fake_streamlit_main)
    monkeypatch.setattr(sys, "argv", ["job-atlas-dashboard"])

    with pytest.raises(SystemExit, match="0"):
        launcher.main()

    app_path = Path(launcher.__file__).with_name("public_app.py")
    assert captured[:3] == [
        "streamlit", "run", str(app_path),
    ]
    assert captured[3:] == [
        "--server.address", "127.0.0.1",
        "--server.port", "8501",
    ]
