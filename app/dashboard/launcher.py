"""Installed command for launching the local dashboard."""
from __future__ import annotations

from pathlib import Path
import sys


def main() -> None:
    from streamlit.web import cli as streamlit_cli

    app_path = Path(__file__).with_name("public_app.py")
    # Pin the public dashboard to IPv4 loopback. On macOS, ``localhost`` may
    # resolve to IPv6 while another Streamlit process owns the same numeric
    # port on IPv6, making a browser refresh reconnect to the wrong app.
    sys.argv = [
        "streamlit", "run", str(app_path),
        "--server.address", "127.0.0.1",
        "--server.port", "8501",
    ]
    raise SystemExit(streamlit_cli.main())
