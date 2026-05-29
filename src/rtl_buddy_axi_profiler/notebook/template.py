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
def _(mo):
    """Reactive seq the WS thread bumps on each inbound selection.

    Reading ``get_selection_seq()`` in the consumer cell below makes
    that cell re-run the instant the background thread calls
    ``set_selection_seq`` — the sub-frame push path (axi-profiler #46).
    Created once and never re-run on its own, so the seq is stable
    across reflows."""
    get_selection_seq, set_selection_seq = mo.state(0)
    return get_selection_seq, set_selection_seq


@app.cell(hide_code=True)
def _(set_selection_seq):
    """Singleton sync handle. ``None`` when ``$RB_HUB_EVENTS_URL`` is
    unset — the rest of the notebook degrades to standalone.

    The ``on_inbound`` hook bumps the reactive seq so an arriving
    selection pushes a re-execution; under a kernel that's sub-frame,
    elsewhere it's a no-op and the poll below takes over."""
    from rtl_buddy_axi_profiler.notebook.sync import from_env

    sync = from_env(on_inbound=lambda: set_selection_seq(lambda v: v + 1))
    return (sync,)


@app.cell(hide_code=True)
def _(mo, sync):
    """Poll backstop — ticks every 500 ms when connected.

    The ``mo.state`` push (above) handles the low-latency path under a
    kernel; this poll is the always-correct fallback for run-mode /
    contexts where the cross-thread setter silently no-ops. No-op cell
    when ``sync`` is ``None``."""
    if sync is None:
        refresher = None
    else:
        refresher = mo.ui.refresh(default_interval="500ms")
        refresher
    return (refresher,)


@app.cell(hide_code=True)
def _(get_selection_seq, refresher, sync):
    """Pull the latest inbound selection from the broker.

    Re-runs on either trigger: the push (``get_selection_seq``, fires
    at arrival) or the poll tick (``refresher``, ≤500 ms backstop)."""
    if sync is None or refresher is None:
        spa_selection = None
    else:
        get_selection_seq()  # push: re-run at arrival
        refresher  # poll: backstop tick
        _, spa_selection = sync.latest_selection
    return (spa_selection,)


@app.cell(hide_code=True)
def _(df):
    """Resolve a hub `selection_changed.instance_path` to a local bundle.

    The SPA publishes the clicked instance's view.json path (e.g.
    `system.dut.out1`); the parquet keys each bundle by `slave_path`,
    so we invert that. `instance_path` may be a list when the hub
    collapsed a multi-driver signal — first match wins."""
    _paths = df.select(["bundle_name", "slave_path"]).unique()
    instance_to_bundle = {
        row["slave_path"]: row["bundle_name"] for row in _paths.iter_rows(named=True)
    }

    def resolve_spa_bundle(sel):
        if not (sel and isinstance(sel, dict)):
            return None
        ip = sel.get("instance_path")
        candidates = ip if isinstance(ip, list) else [ip]
        for c in candidates:
            if c in instance_to_bundle:
                return instance_to_bundle[c]
        return None

    return (resolve_spa_bundle,)


@app.cell(hide_code=True)
def _(bundle_dd, resolve_spa_bundle, spa_selection):
    """SPA-driven selection wins when it resolves to a bundle this
    parquet carries; otherwise fall back to the dropdown."""
    bundle_from_spa = resolve_spa_bundle(spa_selection)
    if bundle_from_spa and bundle_from_spa in bundle_dd.options:
        selected_bundle = bundle_from_spa
    else:
        selected_bundle = None if bundle_dd.value == "(all)" else bundle_dd.value
    return (selected_bundle,)


@app.cell(hide_code=True)
def _(bundle_dd, mo, resolve_spa_bundle, spa_selection):
    """Surface SPA→notebook divergence.

    Without this, an SPA click on an instance this parquet doesn't
    carry silently falls back to the dropdown — indistinguishable
    from "the wire is broken" or "the user mis-clicked". A callout
    here at least tells them which instance the SPA *thought* they
    wanted and that the notebook stayed on its own selection."""
    if spa_selection and isinstance(spa_selection, dict):
        _ip = spa_selection.get("instance_path")
        if _ip and resolve_spa_bundle(spa_selection) is None:
            _shown = _ip if isinstance(_ip, str) else ", ".join(str(x) for x in _ip)
            mo.callout(
                mo.md(
                    f"SPA selected **`{_shown}`** — no matching bundle in "
                    f"this parquet. Showing dropdown selection "
                    f"(`{bundle_dd.value}`) instead."
                ),
                kind="warn",
            )
    return


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
def _(df, mo, selected_bundle):
    """Brushable timeline. Wrapped in ``mo.ui.altair_chart`` so the
    interval selection is reactive — the publisher cell below reads
    ``timeline_chart.value`` (rows under the brush) and pushes the
    span to the SPA as a ``time-window`` envelope."""
    from rtl_buddy_axi_profiler.notebook.plots import timeline

    timeline_chart = mo.ui.altair_chart(timeline(df, bundle=selected_bundle))
    timeline_chart
    return (timeline_chart,)


@app.cell(hide_code=True)
def _(sync, timeline_chart):
    """Push the brush span to the SPA whenever the user adjusts it.

    ``mo.ui.altair_chart(...).value`` is the dataframe of rows that
    fall inside the brush. Empty (or ``None``) means no active
    selection — nothing to publish. The notebook→SPA wire carries
    femtoseconds, and the parquet stores picoseconds, so we convert
    at the boundary (1 ps = 1000 fs)."""
    if sync is not None and timeline_chart.value is not None:
        rows = timeline_chart.value
        if len(rows) > 0:
            lo_ps = float(rows["t_start_ps"].min())
            hi_ps = float(rows["t_end_ps"].max())
            sync.publish_time_window(int(lo_ps * 1000), int(hi_ps * 1000))
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
