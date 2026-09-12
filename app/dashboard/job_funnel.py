"""Read and render the ingestion half of the Decision Run job funnel."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go


def render_job_funnel(renderer, flow: pd.DataFrame) -> None:
    """Render a flow-style sequence without making ``main`` own its shape."""
    if flow.empty:
        renderer.info("Job funnel telemetry is unavailable for this run.")
        return
    chart_flow = flow.copy()
    for column in ("input", "advanced", "dropped", "failed", "pending"):
        chart_flow[column] = pd.to_numeric(chart_flow[column], errors="coerce")
    serial = chart_flow.dropna(subset=["input", "advanced"])
    if serial.empty:
        renderer.info("Job funnel telemetry is unavailable for this run.")
        return
    labels = [str(serial.iloc[0]["from"])]
    sources, targets, values = [], [], []
    node_index = {labels[0]: 0}
    for _, row in serial.iterrows():
        if str(row["stage"]) == "screening_ranking_grouping":
            for key, label in (
                ("eligible", "Eligible"),
                ("anomalies", "Anomalies"),
                ("rejected", "Rejected"),
            ):
                count = row.get(key)
                if pd.isna(count) or not int(count):
                    continue
                node_index[label] = len(labels)
                labels.append(label)
                sources.append(node_index[str(row["from"])])
                targets.append(node_index[label])
                values.append(int(count))
            continue
        output = str(row["to"])
        if output not in node_index:
            node_index[output] = len(labels)
            labels.append(output)
        sources.append(node_index[str(row["from"])])
        targets.append(node_index[output])
        values.append(int(row["advanced"]))
        for outcome in ("dropped", "failed", "pending"):
            if pd.isna(row[outcome]):
                continue
            count = int(row[outcome])
            if not count:
                continue
            outcome_label = f"{str(row['stage']).replace('_', ' ').title()} — {outcome.title()}"
            node_index[outcome_label] = len(labels)
            labels.append(outcome_label)
            sources.append(node_index[str(row["from"])])
            targets.append(node_index[outcome_label])
            values.append(count)
    renderer.plotly_chart(go.Figure(go.Sankey(
        node={"label": labels, "pad": 18, "thickness": 18},
        link={"source": sources, "target": targets, "value": values},
    )), width="stretch")
    renderer.dataframe(flow, width="stretch", hide_index=True)
