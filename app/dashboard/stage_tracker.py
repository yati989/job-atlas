"""Rendering boundary for decision-run stage progress.

The Streamlit entrypoint can hand this seam a renderer without coupling later
stages to the large dashboard module.
"""
from __future__ import annotations

import pandas as pd

from app.decision_runs.progress_table import stage_summary_table


def render_stage_tracker(renderer, table: pd.DataFrame) -> None:
    """Read-only stage summary with expandable, append-only attempt history."""
    # Retain the tiny generic rendering seam used by callers that do not hand
    # us a durable stage projection (and by the original foundation test).
    if "stage" not in table.columns:
        renderer.dataframe(table, width="stretch", hide_index=True)
        return
    if table.empty:
        renderer.info("No durable stage telemetry is available for this run.")
        return
    renderer.dataframe(
        stage_summary_table(table), width="stretch", hide_index=True,
    )
    for row in table.itertuples(index=False):
        history = getattr(row, "attempt_history", [])
        with renderer.expander(f"{str(row.stage).replace('_', ' ').title()} — {len(history)} attempt(s)"):
            renderer.dataframe(pd.DataFrame(history), width="stretch", hide_index=True)
