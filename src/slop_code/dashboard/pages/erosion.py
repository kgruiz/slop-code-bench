"""Retired erosion page."""

import dash
import dash_bootstrap_components as dbc
from dash import html

dash.register_page(__name__, path="/erosion")

layout = dbc.Container(
    dbc.Alert(
        [
            html.H4("Erosion Metrics Retired", className="alert-heading"),
            html.P(
                "Delta-mass and symbol-churn metrics were removed from checkpoint exports."
            ),
            html.P(
                "Use the comparison and quality pages for the remaining CC and mass.cc views.",
                className="mb-0",
            ),
        ],
        color="secondary",
        className="mt-4",
    ),
    fluid=True,
    className="py-3",
)
