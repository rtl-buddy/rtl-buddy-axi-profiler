"""Marimo notebook for axi-perf deep-dive.

Run with::

    AXI_TXNS_PARQUET=/path/to/axi-txns.parquet marimo edit \\
        -p rtl_buddy_axi_profiler/notebook/template.py

Or via the launcher (rtl_buddy #182, follow-up)::

    rb axi-profile notebook <test>

The notebook loads the parquet (from #17's emit stage), shows a
header summary, and renders the six v1 drill-down plots. Each plot
is a reactive cell — the bundle dropdown and timeline brush feed
the rest of the cells, so changing one filter reflows the others.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo>=0.9", "altair>=5", "polars>=1.0", "pyarrow>=14"]
# ///

import marimo

__generated_with = "0.23.7"
app = marimo.App(width="medium", app_title="axi-perf drill-down")


@app.cell(hide_code=True)
def _():
    import marimo as mo
    import os

    return mo, os


@app.cell(hide_code=True)
def _(mo, os):
    """Header — show the loaded parquet path, fail fast if unset."""
    path = os.environ.get("AXI_TXNS_PARQUET")
    if not path:
        mo.md(
            "**No `AXI_TXNS_PARQUET` set.** Launch via `rb axi-profile notebook` "
            "or export the env var by hand, e.g. "
            "`AXI_TXNS_PARQUET=/path/to/axi-txns.parquet marimo edit ...`"
        )
        mo.stop(True, mo.md("Aborting — no parquet to load."))
    parquet_path = path
    return (parquet_path,)


@app.cell(hide_code=True)
def _(mo, parquet_path):
    """Load the parquet + render a one-line summary."""
    import polars as pl

    df = pl.read_parquet(parquet_path)
    mo.md(
        f"Loaded **{parquet_path}** — {df.height:,} transactions, "
        f"{df['bundle_name'].n_unique()} bundles."
    )
    return df, pl


@app.cell(hide_code=True)
def _(df, mo):
    """Bundle selector — feeds every per-bundle plot below."""
    bundle_dd = mo.ui.dropdown(
        options=["(all)"] + sorted(df["bundle_name"].unique().to_list()),
        value="(all)",
        label="bundle",
    )
    bundle_dd
    return (bundle_dd,)


@app.cell(hide_code=True)
def _(bundle_dd):
    selected_bundle = None if bundle_dd.value == "(all)" else bundle_dd.value
    return (selected_bundle,)


@app.cell(hide_code=True)
def _(df, mo):
    """Downsample warning when full timeline / outstanding-depth
    would be slow to render."""
    from rtl_buddy_axi_profiler.notebook.plots import DOWNSAMPLE_THRESHOLD

    if df.height > DOWNSAMPLE_THRESHOLD:
        mo.callout(
            mo.md(
                f"**{df.height:,} rows** exceeds the snappy-render budget "
                f"({DOWNSAMPLE_THRESHOLD:,}). Timeline + outstanding-depth "
                "below auto-stride-sample; CDF / heatmap / fairness / "
                "throughput run on the full set."
            ),
            kind="warn",
        )
    return


@app.cell(hide_code=True)
def _(df, selected_bundle):
    from rtl_buddy_axi_profiler.notebook.plots import timeline

    timeline(df, bundle=selected_bundle)
    return


@app.cell(hide_code=True)
def _(df, selected_bundle):
    from rtl_buddy_axi_profiler.notebook.plots import latency_cdf

    latency_cdf(df, bundle=selected_bundle)
    return


@app.cell(hide_code=True)
def _(df, selected_bundle):
    from rtl_buddy_axi_profiler.notebook.plots import outstanding_depth

    outstanding_depth(df, bundle=selected_bundle)
    return


@app.cell(hide_code=True)
def _(df, selected_bundle):
    from rtl_buddy_axi_profiler.notebook.plots import id_heatmap

    id_heatmap(df, bundle=selected_bundle)
    return


@app.cell(hide_code=True)
def _(df):
    """Fairness is interconnect-level — bundle filter intentionally
    doesn't apply here (a single bundle has no fairness peers)."""
    from rtl_buddy_axi_profiler.notebook.plots import fairness

    fairness(df)
    return


@app.cell(hide_code=True)
def _(df, pl, selected_bundle):
    from rtl_buddy_axi_profiler.notebook.plots import throughput

    if selected_bundle is None:
        out = throughput(df)
    else:
        out = throughput(df.filter(pl.col("bundle_name") == selected_bundle))
    out
    return


if __name__ == "__main__":
    app.run()
