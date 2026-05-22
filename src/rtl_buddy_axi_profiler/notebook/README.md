# `rtl_buddy_axi_profiler.notebook` — marimo deep-dive

Companion package for the `axi-txns.parquet` artifact emitted by
`rb axi-profile run --emit-txns-parquet` (issue #17). Provides:

- `plots.py` — pure altair-chart functions, importable from any
  script / notebook / test.
- `template.py` — a marimo notebook that wires the plots into
  reactive cells with bundle dropdowns + a brushable timeline.

## Install

```
pip install 'rtl-buddy-axi-profiler[notebook]'
```

This pulls in `marimo`, `altair`, `polars`, and `pyarrow`. The
parent package stays importable without the extra — only this
subpackage's modules require it.

## Run the notebook

```bash
AXI_TXNS_PARQUET=/path/to/axi-txns.parquet \
  marimo edit -p $(python -c 'import rtl_buddy_axi_profiler.notebook.template as t; print(t.__file__)')
```

Or, once it lands, via the launcher (rtl_buddy #182):

```bash
rb axi-profile notebook <test>
```

The launcher resolves the test's parquet, sets `$AXI_TXNS_PARQUET`,
and `marimo edit`s against this template.

## v1 plots

| Function | What it shows | Default filter |
|---|---|---|
| `timeline` | per-bundle ticks over time, brushable | bundle dropdown |
| `latency_cdf` | empirical CDF of `ar_to_r_first_cyc` / `aw_to_b_cyc` | bundle dropdown |
| `outstanding_depth` | inflight count from start/end overlaps | bundle dropdown |
| `id_heatmap` | per-bundle × `txn_id` count + mean latency | bundle dropdown |
| `fairness` | sliding-window Jain index | none (cross-bundle by design) |
| `throughput` | rolling-window bits/s | bundle dropdown |

Each plot is a pure function: `f(df, *, bundle=None, ...) -> alt.Chart`.

### Auto-downsampling

`timeline` and `outstanding_depth` stride-sample if the input exceeds
`DOWNSAMPLE_THRESHOLD = 50_000` rows. CDF / heatmap / fairness /
throughput aggregate, so they stay performant on the full set. The
notebook surfaces a banner when the threshold trips.

## How to add a new plot

1. Add a pure function to `plots.py`:

   ```python
   def my_new_plot(df: Any, *, bundle: str | None = None) -> Any:
       """One-line summary; longer note on what the plot answers."""
       alt, pl = _imports()
       df = _ensure_polars(df, pl)
       df = _filter_bundle(df, bundle, pl)
       # ... shape the data ...
       return alt.Chart(df.to_pandas()).mark_point().encode(...).properties(title=...)
   ```

2. Export it via the `__all__` list at the bottom of `plots.py`.

3. Add a smoke test in `tests/test_notebook_plots.py`:

   ```python
   def test_my_new_plot_returns_chart(sample_df, alt):
       chart = plots.my_new_plot(sample_df)
       assert isinstance(chart, alt.Chart)
   ```

4. Wire a reactive cell in `template.py`:

   ```python
   @app.cell
   def _(df, selected_bundle):
       from rtl_buddy_axi_profiler.notebook.plots import my_new_plot
       my_new_plot(df, bundle=selected_bundle)
       return
   ```

The `df` parameter in `template.py` is already a polars `DataFrame`
loaded from `$AXI_TXNS_PARQUET`; the `selected_bundle` value comes
from the bundle dropdown cell. New cells inherit the same reactive
filter chain for free.

## Schema reference

The parquet columns the plots assume:

| column | type | source |
|---|---|---|
| `bundle_name` | str | manifest |
| `is_read` | bool | reconstruct |
| `txn_id` | i64 | reconstruct |
| `addr`, `len_beats`, `size_log2` | int | reconstruct |
| `t_start_fs`, `t_first_data_fs`, `t_end_fs` | i64 | ingest |
| `resp` | i8 | reconstruct |
| `ar_to_r_first_cyc`, `aw_to_b_cyc` | i64? | derived |
| `master_path`, `slave_path` | str | manifest |

See `stages/emit/txns_parquet_v1.py` for the producer side and the
v1.0 schema lock.
