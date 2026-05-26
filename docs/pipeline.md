# Pipeline architecture

`rtl-buddy-axi-profiler` is a five-stage pipeline. Each stage is a Python `Protocol` (see [`stages/protocol.py`](../src/rtl_buddy_axi_profiler/stages/protocol.py)); variants register via the `rtl_buddy_axi_profiler.stages` entry-point group and are selected at runtime via CLI flags.

```
  ┌──────────┐   ┌──────────┐   ┌───────────────┐   ┌─────────────┐   ┌────────┐
  │ discover │ → │  ingest  │ → │  reconstruct  │ → │  aggregate  │ → │  emit  │
  └──────────┘   └──────────┘   └───────────────┘   └─────────────┘   └────────┘
   filelist       FST | VCD       HandshakeEvent     per-bundle         axi-perf.json
   + top          | stream        → Transaction      stats              v1
   → Manifest     → events        (AR/AW/R/W/B)      + log-hist
                                                     + roll-up
```

## Stage Protocols

| Stage         | Input                  | Output             | v1 impls           | Future variants                  |
|---------------|------------------------|--------------------|--------------------|-----------------------------------|
| `Discover`    | filelist + top         | `Manifest`         | `verible` (#2)     | manifest-only                     |
| `Ingest`      | source + manifest      | `HandshakeEvent`*  | `fst`, `vcd` (#3)  | `stream` (#4), cocotb monitor     |
| `Reconstruct` | events                 | `Transaction`*     | `axi4` (#3)        | `axi-lite`, `axi5`                |
| `Aggregate`   | txns + manifest        | `AggregateStats`   | `standard` (#3)    | windowed / time-series            |
| `Emit`        | stats + manifest + out | (file)             | `json-v1` (#3)     | (additive only — v1 is locked)    |

\* iterators — never materialize the full list. Long sims would overrun memory.

## Stage selection

```
axi-profiler run --filelist f --top X \
  --discover verible --ingest fst --reconstruct axi4 \
  --aggregate standard --emit json-v1 \
  -i foo.fst -o axi-perf.json
```

Defaults are `verible / fst / axi4 / standard / json-v1`, so the common form is:

```
axi-profiler run --filelist f --top X -i foo.fst -o axi-perf.json
```

For long-run sims where FST gets unwieldy, swap the ingest:

```
axi-profiler gen-monitor axi-bundles.yaml -o axi_perf_mon.sv
# ... compile mon.sv into the TB, run sim, get foo.axis ...
axi-profiler run --filelist f --top X --ingest stream -i foo.axis -o axi-perf.json
```

## Why the split

- **Independent ownership**: each stage's variants can be written, reviewed, and replaced in isolation.
- **Pure-function chain**: side effects only at the I/O endpoints (CLI for input, emit for output). The reconstruct/aggregate stages are testable as plain functions over iterators.
- **Wire contracts at the boundaries**: every stage I/O is either a typed dataclass (in-memory) or a JSON / YAML / binary file with a locked schema (across processes). No ad-hoc shapes between stages.

## Schemas (locked v1)

- [`axi-perf.json`](axi-perf-schema.md) — the consumer-facing JSON. Read by `rtl-buddy-view`'s `axi-perf` overlay.
- [`axi-bundles.yaml`](axi-bundles-schema.md) — the manifest. Produced by `discover`, consumed by every later stage.
- [`axi-stream`](axi-stream-format.md) — the packed-binary intermediate emitted by the generated SV monitor and read by `StreamIngest`.

All three are locked at v1.0; additive evolution only post-v1.

## Path-specific docs

- [`fst-pipeline.md`](fst-pipeline.md) — wellen-backed FST/VCD ingest path: signal-naming gotchas, posedge sampling model, measured runtime budget, and the canonical trust-set fixtures under `tests/fixtures/e2e/`.

## Cross-repo coupling

This pipeline's outputs feed three downstream consumers:

1. **`rtl-buddy-view` Phase 11** — reads `axi-perf.json` for the `axi-perf` overlay.
2. **`rtl_buddy`** — wraps this tool's CLI as `rb axi-profile`.
3. **The generated SV monitor** (`gen-monitor`) — emits the `axi-stream` binary that `StreamIngest` reads. Producer and consumer must match exactly on record layout.
