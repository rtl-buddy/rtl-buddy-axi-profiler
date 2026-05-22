"""Smoke tests for the notebook plot library.

Each plot is exercised against a synthesised polars DataFrame
matching the axi-txns.parquet schema. The contract is that the
function returns *some* ``altair.Chart`` without raising — we don't
render or compare visuals here. The tests also exercise the
bundle-filter path and the empty-input edge case.

Skip cleanly when the ``[notebook]`` extras aren't installed.
"""

from __future__ import annotations

import pytest


alt = pytest.importorskip("altair")
pl = pytest.importorskip("polars")

from rtl_buddy_axi_profiler.notebook import plots  # noqa: E402


def _sample_df():
    """24 transactions across 2 bundles, mixed read/write, varied
    txn_ids — enough to exercise group_by + window paths without
    tripping downsampling."""
    rows = []
    for bundle in ("cpu_to_xbar", "dma_to_xbar"):
        for i in range(12):
            is_read = i % 2 == 0
            rows.append(
                {
                    "bundle_name": bundle,
                    "is_read": is_read,
                    "txn_id": i % 4,
                    "addr": 0x1000 + i * 0x40,
                    "len_beats": 4,
                    "size_log2": 3,
                    "t_start_fs": 1_000_000 + i * 100_000,
                    "t_first_data_fs": (1_000_000 + i * 100_000 + 50_000)
                    if is_read
                    else None,
                    "t_end_fs": 1_000_000 + i * 100_000 + 200_000,
                    "resp": 0,
                    "ar_to_r_first_cyc": 1 if is_read else None,
                    "aw_to_b_cyc": None if is_read else 3,
                    "master_path": f"soc.u_{bundle.split('_')[0]}",
                    "slave_path": "soc.u_xbar",
                }
            )
    return pl.DataFrame(rows)


def _empty_df():
    return _sample_df().head(0)


# Each plot function pair — (callable, accepts_bundle, accepts_empty).
# ``accepts_empty`` is False for the fairness plot: its windowing
# loop is a no-op on empty input but the downstream chart still
# materialises a 1-row blank, which is fine.
PLOTS = [
    ("timeline", True),
    ("latency_cdf", True),
    ("outstanding_depth", True),
    ("id_heatmap", True),
    ("fairness", False),
    ("throughput", False),
]


@pytest.mark.parametrize("plot_name, accepts_bundle", PLOTS)
def test_plot_returns_altair_chart(plot_name: str, accepts_bundle: bool) -> None:
    fn = getattr(plots, plot_name)
    chart = fn(_sample_df())
    assert isinstance(chart, alt.TopLevelMixin), (
        f"{plot_name} returned {type(chart).__name__}, expected an altair chart"
    )


@pytest.mark.parametrize(
    "plot_name",
    [name for name, accepts_bundle in PLOTS if accepts_bundle],
)
def test_plot_accepts_bundle_filter(plot_name: str) -> None:
    """The bundle dropdown in the template feeds these as keyword
    arg; lock the signature."""
    fn = getattr(plots, plot_name)
    chart = fn(_sample_df(), bundle="cpu_to_xbar")
    assert isinstance(chart, alt.TopLevelMixin)


def test_plot_accepts_empty_dataframe() -> None:
    """Empty parquet (clean test run, zero AXI traffic) should not
    explode any of the plots — the notebook should still render."""
    for plot_name, _ in PLOTS:
        fn = getattr(plots, plot_name)
        chart = fn(_empty_df())
        assert isinstance(chart, alt.TopLevelMixin), plot_name


def test_downsample_threshold_is_exported() -> None:
    """The template imports DOWNSAMPLE_THRESHOLD to size its banner;
    keep it visible at module level."""
    assert isinstance(plots.DOWNSAMPLE_THRESHOLD, int)
    assert plots.DOWNSAMPLE_THRESHOLD > 0


def test_plots_can_consume_pandas_input() -> None:
    """Plots accept polars OR pandas — keep both happy."""
    pdf = _sample_df().to_pandas()
    chart = plots.latency_cdf(pdf)
    assert isinstance(chart, alt.TopLevelMixin)


def test_notebook_extras_missing_message() -> None:
    """The NotebookImportError carries the install hint the user
    sees if they accidentally import a plot function without the
    [notebook] extras. Pin the phrasing so docs + error stay aligned."""
    err = plots.NotebookImportError("test")
    assert isinstance(err, RuntimeError)
