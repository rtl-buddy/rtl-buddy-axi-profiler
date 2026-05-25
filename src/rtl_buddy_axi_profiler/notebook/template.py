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
# dependencies = [
#   "marimo>=0.9",
#   "altair>=5",
#   "polars>=1.0",
#   "pyarrow>=14",
#   "websockets>=12",
# ]
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
def _():
    """Singleton sync handle. ``None`` when ``$RB_HUB_EVENTS_URL`` is
    unset — the rest of the notebook degrades to standalone."""
    from rtl_buddy_axi_profiler.notebook.sync import from_env

    sync = from_env()
    return (sync,)


@app.cell(hide_code=True)
def _(mo, sync):
    """Tick the sync-poller every 500 ms when connected.

    Polling (vs. cross-thread ``mo.state`` setters) keeps the bridge
    between the background WS thread and marimo's reactive loop
    boring. No-op cell when ``sync`` is ``None``."""
    if sync is None:
        refresher = None
    else:
        refresher = mo.ui.refresh(default_interval="500ms")
        refresher
    return (refresher,)


@app.cell(hide_code=True)
def _(refresher, sync):
    """Pull the latest inbound selection from the broker."""
    if sync is None or refresher is None:
        spa_selection = None
    else:
        refresher  # depend on the tick
        _, spa_selection = sync.latest_selection
    return (spa_selection,)


@app.cell(hide_code=True)
def _(bundle_dd, spa_selection):
    """SPA-driven selection wins when present and known to this parquet."""
    if spa_selection and isinstance(spa_selection, dict):
        bundle_from_spa = spa_selection.get("bundle")
        if bundle_from_spa and bundle_from_spa in bundle_dd.options:
            selected_bundle = bundle_from_spa
        else:
            selected_bundle = None if bundle_dd.value == "(all)" else bundle_dd.value
    else:
        selected_bundle = None if bundle_dd.value == "(all)" else bundle_dd.value
    return (selected_bundle,)


@app.cell(hide_code=True)
def _(mo, sync):
    """Time-window publisher — pushes a ``time-window`` envelope to
    the SPA when the user enters start/end and clicks publish.

    A future PR can replace this with brush state harvested from
    an altair chart; this manual form is enough to prove the
    notebook→SPA path end-to-end."""
    publish_btn = None
    if sync is not None:
        t_start = mo.ui.number(value=0, label="t_start_fs")
        t_end = mo.ui.number(value=0, label="t_end_fs")

        def _publish(_value):
            sync.publish_time_window(int(t_start.value), int(t_end.value))

        publish_btn = mo.ui.button(label="publish window to SPA", on_click=_publish)
        mo.hstack([t_start, t_end, publish_btn])
    return (publish_btn,)


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
