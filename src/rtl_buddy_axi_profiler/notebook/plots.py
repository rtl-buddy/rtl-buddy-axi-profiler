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


def _pick_time_unit(span_ps: float) -> tuple[float, str]:
    """Choose a display divisor + axis label for a ps-based timestamp span.

    The parquet schema stores timestamps in picoseconds (good for sub-ns
    sim clocks), but a 100 μs sim's tick labels would be eight-digit
    numbers if we plotted ps directly. Switch units at each 1000× boundary
    so the visible numbers stay 1–3 digits before the decimal.

      span <    1 ns → ps
      span <    1 μs → ns
      span <    1 ms → μs
      else           → ms

    ``span_ps`` of 0 falls through to ps (empty / single-row dataframes
    have nothing meaningful to scale anyway).
    """
    if span_ps >= 1e9:
        return 1e9, "t (ms)"
    if span_ps >= 1e6:
        return 1e6, "t (μs)"
    if span_ps >= 1e3:
        return 1e3, "t (ns)"
    return 1.0, "t (ps)"


def _time_span_ps(df: Any, pl: Any, *col_names: str) -> float:
    """Best-effort span across one or more time columns. Returns 0 when
    every column is empty (no rows) so callers fall back to ps."""
    lo: float | None = None
    hi: float | None = None
    for col in col_names:
        if col not in df.columns:
            continue
        s = df[col].drop_nulls()
        if s.is_empty():
            continue
        c_lo = s.min()
        c_hi = s.max()
        lo = c_lo if lo is None else min(lo, c_lo)
        hi = c_hi if hi is None else max(hi, c_hi)
    if lo is None or hi is None:
        return 0.0
    return float(hi - lo)


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

    divisor, label = _pick_time_unit(_time_span_ps(df, pl, "t_start_ps", "t_end_ps"))
    df = df.with_columns(t_start=(pl.col("t_start_ps") / divisor))
    brush = alt.selection_interval(encodings=["x"], name="timeline_brush")
    return (
        alt.Chart(df)
        .mark_tick(thickness=2, opacity=0.6)
        .encode(
            x=alt.X("t_start:Q", title=label),
            y=alt.Y("bundle_name:N", title="bundle"),
            color=alt.condition(
                brush, alt.Color("is_read:N", title="read?"), alt.value("lightgray")
            ),
            tooltip=[
                "bundle_name",
                "is_read",
                "txn_id",
                "addr",
                alt.Tooltip("len_beats:Q", title="len (beats)"),
                "resp",
                alt.Tooltip("t_start_ps:Q", title="t_start (ps)"),
                alt.Tooltip("t_end_ps:Q", title="t_end (ps)"),
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
            y=alt.Y("cdf:Q", title="empirical CDF (fraction of txns)"),
            color=alt.Color("kind:N", title="latency type"),
            tooltip=[
                "kind",
                alt.Tooltip("cycles:Q", title="latency (cyc)"),
                alt.Tooltip("cdf:Q", format=".3f", title="CDF (fraction)"),
            ],
        )
        .properties(height=240, title="Latency CDF (per transaction)")
        .interactive()
    )


def outstanding_depth(df: Any, *, bundle: str | None = None) -> Any:
    """Inflight transaction count per bundle, derived from
    ``t_start_ps`` / ``t_end_ps`` overlaps.

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
            pl.col("t_start_ps").alias("t_ps"),
            pl.lit(1).alias("delta"),
        ]
    )
    ends = df.select(
        [
            pl.col("bundle_name"),
            pl.col("t_end_ps").alias("t_ps"),
            pl.lit(-1).alias("delta"),
        ]
    )
    events = pl.concat([starts, ends]).sort(["bundle_name", "t_ps"])
    cumulative = events.with_columns(
        depth=pl.col("delta").cum_sum().over("bundle_name")
    )

    if df.height > DOWNSAMPLE_THRESHOLD:
        stride = max(1, cumulative.height // DOWNSAMPLE_THRESHOLD)
        cumulative = cumulative.gather_every(stride)

    divisor, label = _pick_time_unit(_time_span_ps(cumulative, pl, "t_ps"))
    cumulative = cumulative.with_columns(t=(pl.col("t_ps") / divisor))
    return (
        alt.Chart(cumulative)
        .mark_area(opacity=0.4, interpolate="step-after")
        .encode(
            x=alt.X("t:Q", title=label),
            y=alt.Y("depth:Q", title="outstanding transactions (count)"),
            color=alt.Color("bundle_name:N", title="bundle"),
            tooltip=[
                "bundle_name",
                alt.Tooltip("t_ps:Q", title="t (ps)"),
                alt.Tooltip("depth:Q", title="outstanding (count)"),
            ],
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
                alt.Tooltip("count:Q", title="# txns"),
                alt.Tooltip("mean_ar_to_r:Q", format=".1f", title="mean AR→R (cyc)"),
                alt.Tooltip("mean_aw_to_b:Q", format=".1f", title="mean AW→B (cyc)"),
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
    df = df.with_columns(bytes_per_txn=bytes_per).sort("t_start_ps")

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
        t_mid = (chunk["t_start_ps"].min() + chunk["t_start_ps"].max()) / 2
        windows.append({"t_ps": t_mid, "jain": jain, "n_bundles": n_b})

    pdf = (
        pl.DataFrame(windows)
        if windows
        else pl.DataFrame(
            schema={"t_ps": pl.Float64, "jain": pl.Float64, "n_bundles": pl.Int64}
        )
    )
    divisor, label = _pick_time_unit(_time_span_ps(pdf, pl, "t_ps"))
    pdf = pdf.with_columns(t=(pl.col("t_ps") / divisor))
    return (
        alt.Chart(pdf)
        .mark_line(point=True)
        .encode(
            x=alt.X("t:Q", title=label),
            y=alt.Y(
                "jain:Q",
                title="Jain fairness index (0–1, 1 = fair)",
                scale=alt.Scale(domain=[0, 1]),
            ),
            tooltip=[
                alt.Tooltip("t_ps:Q", title="window mid (ps)"),
                alt.Tooltip("jain:Q", format=".3f", title="Jain index"),
                alt.Tooltip("n_bundles:Q", title="# active bundles"),
            ],
        )
        .properties(height=200, title=f"Sliding-window fairness ({window_size}-txn)")
        .interactive()
    )


def throughput(df: Any, *, window_ps: int = 1_000_000) -> Any:
    """Windowed throughput per bundle, drawn as a step function in GB/s.

    Bins transactions by ``t_start_ps`` into fixed windows of
    ``window_ps`` picoseconds, sums the bytes that *started* in each
    window, and divides by the window length to get the average
    throughput **during** that window — reported in **gigabytes per
    second** (the natural unit for modern AXI fabrics, where realistic
    wire speeds land in the multi-GB/s range). The underlying
    axi-perf.json roll-up still reports bits/sec; this plot converts at
    the display boundary (``GB/s = bytes_per_s / 1e9``).

    **Step, not line.** Each bin's value is an average over the whole
    window, not a sample at an instant, so it is drawn as a flat level
    spanning the window rather than a sloped line between points. That
    forces a choice the docstring pins down deliberately:

      * The x-coordinate of each point is the window's **left edge**
        (``bin * window_ps``) — the moment the averaging window opens.
      * The mark uses ``interpolate="step-after"``, which holds a
        point's level from its own x rightward to the next point's x.
        With x at the left edge, that level covers exactly
        ``[bin*W, (bin+1)*W)`` — the window it measures.

    So the step transitions sit on the **window boundaries (between the
    binned points), not centered on them**. Centering the step
    (``interpolate="step"`` with the point at the bin midpoint) would
    imply the throughput was *sampled at the midpoint*; but it is an
    integral over the entire window, and any idle window would then put
    the transition half a window off. Edge-anchored ``step-after`` is
    the faithful representation.

    **Idle windows read 0, not the last level.** A window with no
    transaction *starts* contributes 0 bytes by this metric (a txn's
    bytes are attributed to the window it starts in), so we densify each
    bundle's bins to a contiguous grid and fill the gaps with 0 — the
    step drops to the floor during idle time instead of coasting at the
    previous level. A closing point per bundle at the right edge of the
    last window makes that final window render at full width (carrying
    its level forward — we make no claim about traffic past the trace).

    Default window is 1 μs — coarse enough to stay readable on multi-ms
    sims but fine enough to spot per-burst variation on 100 ns – 10 μs
    runs. Pass ``window_ps`` to override.
    """
    alt, pl = _imports()
    df = _ensure_polars(df, pl)

    bytes_per = (pl.col("len_beats") * pl.lit(2).pow(pl.col("size_log2"))).cast(
        pl.Int64
    )
    binned = df.with_columns(
        [
            bytes_per.alias("bytes_per_txn"),
            (pl.col("t_start_ps") // window_ps).alias("bin"),
        ]
    )
    agg = binned.group_by(["bundle_name", "bin"]).agg(
        pl.col("bytes_per_txn").sum().alias("bytes")
    )

    # Densify: fill windows that saw no transaction starts with 0 bytes so
    # the step drops to the floor during idle time rather than coasting at
    # the last level. Each bundle gets a contiguous bin grid [min, max].
    if agg.height > 0:
        bounds = agg.group_by("bundle_name").agg(
            pl.col("bin").min().alias("bin_lo"),
            pl.col("bin").max().alias("bin_hi"),
        )
        grid = (
            bounds.with_columns(
                bin=pl.int_ranges(pl.col("bin_lo"), pl.col("bin_hi") + 1)
            )
            .explode("bin")
            .select("bundle_name", "bin")
        )
        agg = grid.join(agg, on=["bundle_name", "bin"], how="left").with_columns(
            pl.col("bytes").fill_null(0)
        )

    # Convert: bytes per window → bytes per second → gigabytes per second.
    # 1 ps = 1e-12 s, so bytes/s = bytes / (window_ps * 1e-12);
    # GB/s = (bytes/s) / 1e9. ``t_ps`` is the window's LEFT EDGE so that
    # ``step-after`` holds the level across the window it belongs to.
    window_s = window_ps * 1e-12
    agg = agg.with_columns(
        gbps=pl.col("bytes") / (window_s * 1e9),
        t_ps=pl.col("bin") * window_ps,
    )

    # Pick the time unit from the real data (before adding closers, so one
    # extra trailing window can't nudge the unit boundary).
    divisor, time_label = _pick_time_unit(_time_span_ps(agg, pl, "t_ps"))

    # Closing edge per bundle: step-after omits the trailing point's
    # rightward level, so append a point at the last window's right edge
    # carrying the same level — renders the final window at full width.
    if agg.height > 0:
        closers = (
            agg.sort(["bundle_name", "bin"])
            .group_by("bundle_name", maintain_order=True)
            .last()
            .with_columns(t_ps=(pl.col("bin") + 1) * window_ps)
        )
        agg = pl.concat([agg, closers])

    agg = agg.with_columns(t=(pl.col("t_ps") / divisor))
    return (
        alt.Chart(agg)
        .mark_line(interpolate="step-after")
        .encode(
            x=alt.X("t:Q", title=time_label),
            y=alt.Y("gbps:Q", title="throughput (GB/s)"),
            color=alt.Color("bundle_name:N", title="bundle"),
            tooltip=[
                "bundle_name",
                alt.Tooltip("t_ps:Q", title="window start (ps)"),
                alt.Tooltip("gbps:Q", format=".3f", title="GB/s"),
            ],
        )
        .properties(
            height=200,
            title=f"Throughput ({window_ps / 1_000_000:g} μs bins, step = avg/window)",
        )
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
