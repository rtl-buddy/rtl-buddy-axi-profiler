# axi-perf.json schema v1.0

Consumer-facing JSON emitted by the `json-v1` emit stage. Read by `rtl-buddy-view`'s `axi-perf` overlay (Phase 11).

Formal JSON Schema: [`src/rtl_buddy_axi_profiler/schema/axi_perf_v1.json`](../src/rtl_buddy_axi_profiler/schema/axi_perf_v1.json). The emit stage runs the payload through this schema before writing — a malformed roll-up never reaches the consumer.

## Top-level shape

```json
{
  "schema_version": "1.0",
  "tool": "rtl-buddy-axi-profiler",
  "tool_version": "0.1.0",
  "produced_at": "2026-05-21T08:00:00Z",
  "design_top": "soc_top",
  "duration_cycles": 1000000,
  "clock_period_ns": 2.0,
  "bundles": [ /* see below */ ],
  "interconnects": [ /* see below */ ]
}
```

`schema_version` is the gate. Consumers reject mismatched versions with a typed error.

## Per-bundle

```json
{
  "name": "cpu_to_soc_xbar",
  "master_path": "soc_top.u_cpu",
  "slave_path": "soc_top.u_soc_xbar",
  "protocol": "AXI4",
  "data_width": 64,
  "id_width": 4,
  "default_view": "parent",
  "channels": {
    "ar": {"util_pct": 32.1, "bp_pct": 4.2,  "peak_occ": 12, "txns": 41023},
    "aw": {"util_pct": 18.7, "bp_pct": 1.1,  "peak_occ":  6, "txns": 22987},
    "r":  {"util_pct": 71.5, "bp_pct": 22.4, "peak_occ": 28, "beats": 328184},
    "w":  {"util_pct": 41.3, "bp_pct": 8.9,  "peak_occ":  9, "beats":  91948},
    "b":  {"util_pct":  9.8, "bp_pct": 0.3,  "peak_occ":  3, "txns": 22987}
  },
  "throughput": {"read_bps": 1.31e9, "write_bps": 0.59e9},
  "outstanding": {"read_peak": 28, "read_avg": 12.4, "write_peak": 9, "write_avg": 3.7},
  "latency_cycles": {
    "ar_to_r_first": {
      "p50": 18, "p95": 76, "p99": 142, "max": 410,
      "hist_log2": [0, 0, 12, 308, 4102, 28311, 7884, 412, 18, 4, 1, 0, 0, 0, 0, 0]
    },
    "aw_to_b": { "p50": 22, "p95": 80, "p99": 160, "max": 512, "hist_log2": [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0] }
  },
  "errors": {"slverr": 0, "decerr": 2},
  "children": [ /* same shape, one level deep in v1 */ ]
}
```

### Channels

Five sub-objects, one per channel. AR / AW / B are request-style (carry `txns`); R / W are data-style (carry `beats`).

| Field      | Type   | Description                                       |
|------------|--------|---------------------------------------------------|
| `util_pct` | number | % of cycles where the channel saw a handshake     |
| `bp_pct`   | number | % of cycles with `valid && !ready` (backpressure) |
| `peak_occ` | int    | Peak observed in-flight on this channel           |
| `txns`     | int    | (AR/AW/B only) completed transaction count        |
| `beats`    | int    | (R/W only) completed beat count                   |

### Latency `hist_log2`

A fixed 16-bucket log-spaced histogram over latency in cycles. Bucket `i` covers `[2^i, 2^(i+1))` cycles, clamped to `[0, 15]`. Counts are integers.

Compact (16 ints / latency / bundle), comparable across bundles, fine resolution where it matters (small-cycle counts dominate AXI latency distributions).

The accompanying `p50` / `p95` / `p99` / `max` are pre-computed by the producer from the raw samples (reservoir-sampled inside the aggregate stage). Consumers should not recompute these from the histogram — the histogram is an approximation.

### `default_view`

Initial-view hint from the producer for hierarchical bundles. The viewer reads it as the initial state and lets the user toggle.

| Value      | Meaning                                                  |
|------------|----------------------------------------------------------|
| `parent`   | Render the parent bundle's edge only; collapse children. |
| `children` | Render each child as a separate edge; hide the parent.   |
| `both`     | Render both layers; user expands the parent to see them. |

### `children[]`

One level deep in v1. Children are *independent measurements*: the parent measures the external interface to an interconnect, children measure its internal sub-bundles. **Parent stats are NOT auto-sums** — the viewer either renders one or the other depending on the user's choice.

Deeper nesting (grandchildren) is reserved for v1.x; v1.0 producers must flatten with a warning.

## Per-interconnect

```json
{
  "node_path": "soc_top.u_soc_xbar",
  "total_read_bps": 4.2e9,
  "total_write_bps": 2.8e9,
  "hottest_master": "soc_top.u_cpu",
  "hottest_slave": "soc_top.u_dram_ctrl",
  "arbitration": {"fairness_jain": 0.78, "starved_masters": []}
}
```

`fairness_jain` is Jain's fairness index across the masters arbitrating at this node (1.0 = perfectly fair). `starved_masters` lists any master whose share of the node's bandwidth fell below a configurable floor.

## Versioning rules

- v1.x evolution is **additive only**. New optional fields keep `schema_version: "1.0"`.
- Breaking changes bump to `schema_version: "2.0"` and ship as a parallel schema file; the emit stage selects by CLI flag.
- The consumer-side overlay (`rtl-buddy-view`) rejects mismatched majors with a typed error, never a stack trace.
