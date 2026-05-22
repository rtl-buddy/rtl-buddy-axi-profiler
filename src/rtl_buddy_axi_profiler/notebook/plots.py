"""Reusable altair specs for the axi-txns.parquet drill-down.

Pure functions — no marimo deps. Each takes a dataframe matching
the ``axi-txns.parquet`` schema (see #17) and returns an
:class:`altair.Chart` ready to display.

The notebook template (:mod:`.template`) wires these into reactive
cells with bundle/channel dropdowns and a brushable timeline; tests
and ad-hoc scripts can import them directly and render however they
want (Jupyter, save as HTML, etc.).

Imports of ``altair`` / ``polars`` are kept at function top so the
parent package stays importable without the ``[notebook]`` extra.
A clear :class:`NotebookImportError` surfaces with an install hint.
"""

from __future__ import annotations

from typing import Any


# Auto-downsample timeline / outstanding-depth above this row count.
# Tuned to keep altair pan/zoom snappy on a typical laptop; the
# template surfaces a banner + slider when it kicks in. CDF /
# heatmap / fairness already aggregate, so they stay performant on
# the full set.
DOWNSAMPLE_THRESHOLD = 50_000


class NotebookImportError(RuntimeError):
    """Raised when the [notebook] extras aren't installed."""


# Public function signatures use ``Any`` to keep the parent package
# importable without the [notebook] extras (no top-level altair /
# polars imports). The docstrings carry the real shape: ``df`` is a
# polars or pandas DataFrame matching the axi-txns.parquet schema;
# returns ``altair.Chart``.


def _imports():
    """Lazy import of altair / polars with a clear install hint."""
    try:
        import altair as alt
        import polars as pl
    except ImportError as e:
        raise NotebookImportError(
            "altair + polars are required for the notebook plot library. "
            "Install with: pip install 'rtl-buddy-axi-profiler[notebook]'"
        ) from e
    return alt, pl


def _ensure_polars(df: Any, pl: Any) -> Any:
    """Accept polars or pandas dataframes — convert pandas to polars.

    Altair 5+ takes polars DataFrames natively (via narwhals), so we
    never need to round-trip through pandas. Pandas only gets
    imported when the *caller* supplied a pandas frame, never by
    the library itself.
    """
    if isinstance(df, pl.DataFrame):
        return df
    return pl.from_pandas(df)


def _filter_bundle(df: Any, bundle: str | None, pl: Any) -> Any:
    if bundle is None:
        return df
    return df.filter(pl.col("bundle_name") == bundle)


def timeline(df: Any, *, bundle: str | None = None) -> Any:
    """Per-bundle handshakes over time, one tick per transaction.

    Faceted by `is_read` (read vs write). Brushable time-window
    selection on the x-axis exposes the selection ID for downstream
    cells to filter on.
    """
    alt, pl = _imports()
    df = _ensure_polars(df, pl)
    df = _filter_bundle(df, bundle, pl)

    if df.height > DOWNSAMPLE_THRESHOLD:
        # Stride-sample. Order-preserving so the brush window still
        # roughly matches the full set's density.
        stride = max(1, df.height // DOWNSAMPLE_THRESHOLD)
        df = df.gather_every(stride)

    brush = alt.selection_interval(encodings=["x"], name="timeline_brush")
    return (
        alt.Chart(df)
        .mark_tick(thickness=2, opacity=0.6)
        .encode(
            x=alt.X("t_start_fs:Q", title="t_start (fs)"),
            y=alt.Y("bundle_name:N", title="bundle"),
            color=alt.condition(
                brush, alt.Color("is_read:N", title="read?"), alt.value("lightgray")
            ),
            tooltip=[
                "bundle_name",
                "is_read",
                "txn_id",
                "addr",
                "len_beats",
                "resp",
                "t_start_fs",
                "t_end_fs",
            ],
        )
        .add_params(brush)
        .properties(height=200, title="AXI transaction timeline")
        .interactive(bind_x=False)  # x is the brush; only zoom y
    )


def latency_cdf(df: Any, *, bundle: str | None = None) -> Any:
    """Empirical CDF of per-transaction latency.

    Two layers — one for read (``ar_to_r_first_cyc``) and one for
    write (``aw_to_b_cyc``). Cycle counts on x, fraction-of-txns on
    y. ``transform_window`` produces the ECDF on the fly so the
    function stays pure / dataframe-shape-only.
    """
    alt, pl = _imports()
    df = _ensure_polars(df, pl)
    df = _filter_bundle(df, bundle, pl)

    # Stack the two latency columns into one long-form frame; layer
    # by `kind` so altair gets a single tidy table.
    reads = df.select(
        [
            pl.col("bundle_name"),
            pl.col("ar_to_r_first_cyc").alias("cycles"),
            pl.lit("ar_to_r_first").alias("kind"),
        ]
    ).filter(pl.col("cycles").is_not_null())
    writes = df.select(
        [
            pl.col("bundle_name"),
            pl.col("aw_to_b_cyc").alias("cycles"),
            pl.lit("aw_to_b").alias("kind"),
        ]
    ).filter(pl.col("cycles").is_not_null())
    long = pl.concat([reads, writes])

    return (
        alt.Chart(long)
        .transform_window(
            sort=[{"field": "cycles"}],
            cdf="cume_dist()",
            groupby=["kind"],
        )
        .mark_line()
        .encode(
            x=alt.X("cycles:Q", title="latency (cycles)"),
            y=alt.Y("cdf:Q", title="empirical CDF"),
            color=alt.Color("kind:N", title="latency type"),
            tooltip=["kind", "cycles", alt.Tooltip("cdf:Q", format=".3f")],
        )
        .properties(height=240, title="Latency CDF (per transaction)")
        .interactive()
    )


def outstanding_depth(df: Any, *, bundle: str | None = None) -> Any:
    """Inflight transaction count per bundle, derived from
    ``t_start_fs`` / ``t_end_fs`` overlaps.

    Each txn contributes +1 at start, -1 at end; cumulative sum is
    the live outstanding count. Plotted as a step-area chart with
    one band per bundle.
    """
    alt, pl = _imports()
    df = _ensure_polars(df, pl)
    df = _filter_bundle(df, bundle, pl)

    # Build the start (+1) / end (-1) event stream.
    starts = df.select(
        [
            pl.col("bundle_name"),
            pl.col("t_start_fs").alias("t_fs"),
            pl.lit(1).alias("delta"),
        ]
    )
    ends = df.select(
        [
            pl.col("bundle_name"),
            pl.col("t_end_fs").alias("t_fs"),
            pl.lit(-1).alias("delta"),
        ]
    )
    events = pl.concat([starts, ends]).sort(["bundle_name", "t_fs"])
    cumulative = events.with_columns(
        depth=pl.col("delta").cum_sum().over("bundle_name")
    )

    if df.height > DOWNSAMPLE_THRESHOLD:
        stride = max(1, cumulative.height // DOWNSAMPLE_THRESHOLD)
        cumulative = cumulative.gather_every(stride)

    return (
        alt.Chart(cumulative)
        .mark_area(opacity=0.4, interpolate="step-after")
        .encode(
            x=alt.X("t_fs:Q", title="t (fs)"),
            y=alt.Y("depth:Q", title="outstanding"),
            color=alt.Color("bundle_name:N"),
            tooltip=["bundle_name", "t_fs", "depth"],
        )
        .properties(height=200, title="Outstanding-depth over time")
        .interactive()
    )


def id_heatmap(df: Any, *, bundle: str | None = None) -> Any:
    """Per-bundle × per-``txn_id`` count + mean latency.

    Color encodes the count (how many transactions used that ID on
    that bundle); tooltip exposes mean ar_to_r_first / aw_to_b in
    cycles for the cell.
    """
    alt, pl = _imports()
    df = _ensure_polars(df, pl)
    df = _filter_bundle(df, bundle, pl)

    agg = df.group_by(["bundle_name", "txn_id"]).agg(
        [
            pl.len().alias("count"),
            pl.col("ar_to_r_first_cyc").mean().alias("mean_ar_to_r"),
            pl.col("aw_to_b_cyc").mean().alias("mean_aw_to_b"),
        ]
    )
    return (
        alt.Chart(agg)
        .mark_rect()
        .encode(
            x=alt.X("txn_id:O", title="txn_id"),
            y=alt.Y("bundle_name:N", title="bundle"),
            color=alt.Color("count:Q", title="# txns"),
            tooltip=[
                "bundle_name",
                "txn_id",
                "count",
                alt.Tooltip("mean_ar_to_r:Q", format=".1f"),
                alt.Tooltip("mean_aw_to_b:Q", format=".1f"),
            ],
        )
        .properties(height=240, title="Transaction ID heatmap")
    )


def fairness(df: Any, *, window_size: int = 256) -> Any:
    """Sliding-window Jain fairness index per bundle.

    Computed over per-bundle byte counts in successive windows of
    ``window_size`` transactions across the full session, so the
    plot shows whether one bundle starves others during bursts.

    Jain index = (Σ x_i)² / (n · Σ x_i²); 1.0 = perfectly fair.
    """
    alt, pl = _imports()
    df = _ensure_polars(df, pl)

    # Approximate bytes-per-txn from len_beats * 2^size_log2.
    bytes_per = (pl.col("len_beats") * pl.lit(2).pow(pl.col("size_log2"))).cast(
        pl.Int64
    )
    df = df.with_columns(bytes_per_txn=bytes_per).sort("t_start_fs")

    # Walk windows. Pure-python loop (window count is small) avoids
    # a complex polars rolling expr that doesn't generalise well to
    # variable bundle membership.
    windows: list[dict[str, Any]] = []
    n = df.height
    bundles = df["bundle_name"].unique().to_list()
    for start in range(0, n, window_size):
        chunk = df.slice(start, window_size)
        if chunk.is_empty():
            continue
        totals: dict[str, int] = {b: 0 for b in bundles}
        sums = chunk.group_by("bundle_name").agg(pl.col("bytes_per_txn").sum())
        for row in sums.iter_rows(named=True):
            totals[row["bundle_name"]] = row["bytes_per_txn"] or 0
        xs = list(totals.values())
        n_b = sum(1 for x in xs if x > 0)
        denom = n_b * sum(x * x for x in xs)
        jain = (sum(xs) ** 2) / denom if denom > 0 else 1.0
        t_mid = (chunk["t_start_fs"].min() + chunk["t_start_fs"].max()) / 2
        windows.append({"t_fs": t_mid, "jain": jain, "n_bundles": n_b})

    pdf = (
        pl.DataFrame(windows)
        if windows
        else pl.DataFrame(
            schema={"t_fs": pl.Float64, "jain": pl.Float64, "n_bundles": pl.Int64}
        )
    )
    return (
        alt.Chart(pdf)
        .mark_line(point=True)
        .encode(
            x=alt.X("t_fs:Q", title="t (fs)"),
            y=alt.Y("jain:Q", title="Jain fairness", scale=alt.Scale(domain=[0, 1])),
            tooltip=["t_fs", alt.Tooltip("jain:Q", format=".3f"), "n_bundles"],
        )
        .properties(height=200, title=f"Sliding-window fairness ({window_size}-txn)")
        .interactive()
    )


def throughput(df: Any, *, window_fs: int = 10_000_000) -> Any:
    """Rolling-window bytes/s per bundle.

    Bins transactions by ``t_start_fs`` into windows of ``window_fs``
    femtoseconds, sums bytes, and reports per-bundle throughput in
    bits/s for symmetry with the dashboard summary.
    """
    alt, pl = _imports()
    df = _ensure_polars(df, pl)

    bytes_per = (pl.col("len_beats") * pl.lit(2).pow(pl.col("size_log2"))).cast(
        pl.Int64
    )
    binned = df.with_columns(
        [
            bytes_per.alias("bytes_per_txn"),
            (pl.col("t_start_fs") // window_fs).alias("bin"),
        ]
    )
    agg = binned.group_by(["bundle_name", "bin"]).agg(
        pl.col("bytes_per_txn").sum().alias("bytes")
    )
    # Per-second normalisation: 1 fs = 1e-15 s, so bps = bytes / (window_fs * 1e-15) * 8.
    agg = agg.with_columns(
        bps=(pl.col("bytes") * 8.0) / (window_fs * 1e-15),
        t_fs=pl.col("bin") * window_fs,
    )
    return (
        alt.Chart(agg)
        .mark_line()
        .encode(
            x=alt.X("t_fs:Q", title="t (fs)"),
            y=alt.Y("bps:Q", title="throughput (bits/s)"),
            color=alt.Color("bundle_name:N"),
            tooltip=[
                "bundle_name",
                "t_fs",
                alt.Tooltip("bps:Q", format=".2e"),
            ],
        )
        .properties(height=200, title=f"Throughput ({window_fs} fs bins)")
        .interactive()
    )


__all__ = [
    "DOWNSAMPLE_THRESHOLD",
    "NotebookImportError",
    "fairness",
    "id_heatmap",
    "latency_cdf",
    "outstanding_depth",
    "throughput",
    "timeline",
]
