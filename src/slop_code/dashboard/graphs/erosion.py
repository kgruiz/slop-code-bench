"""Retired erosion graphs for removed delta-mass metrics."""

from __future__ import annotations

import plotly.graph_objects as go

from slop_code.dashboard.data import ChartContext


def _retired_figure(title: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(title=title)
    fig.add_annotation(
        text="Retired: delta-mass and symbol-churn metrics are no longer exported.",
        showarrow=False,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
    )
    return fig


def build_mass_delta_bars(context: ChartContext) -> go.Figure:
    return _retired_figure("Erosion Overview")


def build_mass_delta_heatmap(context: ChartContext) -> go.Figure:
    return _retired_figure("Erosion Heatmap")


def build_delta_vs_solve_scatter(context: ChartContext) -> go.Figure:
    return _retired_figure("Erosion vs Solve Rate")


def build_mass_delta_boxplots(context: ChartContext) -> go.Figure:
    return _retired_figure("Erosion Distributions")


def build_other_mass_metrics(context: ChartContext) -> go.Figure:
    return _retired_figure("Additional Erosion Metrics")


def build_velocity_metrics(context: ChartContext) -> go.Figure:
    return _retired_figure("Erosion Velocity")


def build_symbol_sprawl(context: ChartContext) -> go.Figure:
    return _retired_figure("Symbol Sprawl")
