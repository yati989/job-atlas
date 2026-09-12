"""Rendering boundary reserved for the decision-run stage funnel."""
from __future__ import annotations

import pandas as pd


def render_stage_funnel(renderer, table: pd.DataFrame) -> None:
    """Render stage counts without making the dashboard entrypoint own the shape."""
    renderer.bar_chart(table, x="stage", y="processed", width="stretch")
