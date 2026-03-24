from __future__ import annotations

import pandas as pd
import pytest

from slop_code.dashboard.data import ChartContext
from slop_code.dashboard.graphs.heatmap import build_single_run_heatmap


def test_build_single_run_heatmap_uses_isolated_pass_rate() -> None:
    context = ChartContext(
        checkpoints=pd.DataFrame(
            [{"problem": "prob1", "idx": 1, "isolated_pass_rate": 0.8}]
        ),
        run_summaries=pd.DataFrame(),
        color_map={},
        base_color_map={},
    )

    fig = build_single_run_heatmap(context)

    assert fig.data[0]["z"][0][0] == pytest.approx(0.8)
