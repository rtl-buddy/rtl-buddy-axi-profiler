# FST/VCD ingest pipeline

Companion to [`pipeline.md`](pipeline.md): zooms in on the wellen-backed ingest path, the signal-naming pitfalls it surfaces, and a measured runtime budget. The five-stage architecture is unchanged; this doc only narrows on what changes when the input is a waveform.

## Pipeline at the FST layer

```
  ┌──────────┐   ┌────────────┐   ┌───────────────┐   ┌─────────────┐   ┌────────┐
  │ discover │ → │  ingest    │ → │  reconstruct  │ → │  aggregate  │ → │  emit  │
  └──────────┘   └────────────┘   └───────────────┘   └─────────────┘   └────────┘
   filelist       wellen.run        axi4 pending      per-bundle         axi-perf.json
   + top          (FST | VCD)       table             stats              (v1, locked)
   → Manifest     → events          → Transaction     + interconnect
                  per-posedge       (AR/AW/R/W/B)     roll-up
                  sampling
```

Only the `ingest` stage looks at the trace file. Every downstream stage operates on Python iterators of `HandshakeEvent` / `Transaction`, so the pipeline is format-agnostic from `reconstruct` onward — wellen reads VCD identically to a Verilator-produced FST. That's why the trust-set fixtures in `tests/fixtures/e2e/` are checked in as text VCDs even though the issue says "FST trust set": the wellen layer eats the same bytes.

## Wellen + posedge sampling

The ingest stage walks the detected clock's posedge times and calls `signal.value_at_time(tick)` on each AXI handshake pair. Every (clock-posedge × bundle × channel) where `valid && ready` is observed emits one `HandshakeEvent`. **It does not scan every value-change record** — that would be O(events × signals); the posedge-driven loop is O(posedges × bundles × channels).

The autodetected global clock is the highest-frequency 1-bit signal in the trace. Multi-clock fabrics fall through to per-bundle `clock_signal` overrides set in `axi-bundles.yaml` (see `tests/fixtures/e2e/*/axi-bundles.yaml` for the pattern).

## Signal-name gotchas

A handful of subtle ways the manifest's `signals:` paths fail to bind to a real FST. Each is the kind of "looks fine in unit tests, breaks in real use" surface the trust-set fixtures exist to catch.

1. **Testbench wrapper prefix.** Sims commonly wrap the DUT under `tb.dut.*` — the manifest emitted by `discover` is rooted at the DUT, not the TB. `tb_prefix` stripping in `WellenIngest(tb_prefix=...)` is the supported escape hatch (see `tests/test_ingest_wellen.py::test_wellen_ingest_strips_tb_prefix_when_set` for the contract).
2. **Interface-modport flattening.** Verible's bundle discovery resolves modport-typed ports as `<inst>.<modport>.<role>` (e.g. `u_cpu.m_axi.arvalid`). When the FST was generated from a design that flattens modports into a top-level signal list (Verilator does, depending on its `--flatten-modports` flag), the path will be `u_cpu.m_axi_arvalid` instead. The discover stage's interface-modport detector handles this — but a hand-written manifest needs both forms checked against the actual FST.
3. **Generate-block instance naming.** `for (genvar i = 0; i < 4; i++) begin : g_ch ...` instantiates as `g_ch[0].u_inst`, `g_ch[1].u_inst`, .... wellen reports those exact strings (brackets and dots). The manifest must use the same string Verilator's `+dumpvars` writes — there's no normalization layer.
4. **`tb_prefix` is per-run, not per-bundle.** If you have one TB wrapping two DUTs in two scopes, you can't strip "two different prefixes" — split the manifest into two runs.

## Acceptance fixtures (canonical examples)

The `tests/fixtures/e2e/` set is the curated "trust set" introduced by issue #31. Each fixture is paired (waveform + manifest + golden JSON):

| Fixture | What it exercises | Acceptance gate |
|---------|-------------------|-----------------|
| `errors/` | `rresp` / `bresp` propagation through aggregate | `errors.slverr` + `errors.decerr` exact match |
| `single_master_single_slave/` | Realistic latency distribution (p50 ≠ p99) | `ar_to_r_first.max > p50` |
| `out_of_order/` | Reconstruct pending-table across IDs | 8/8 read+write txns reconstructed; truth table in `expected_latencies.txt` |
| `crossbar_2x2/` | Multi-master interconnect rollup | `total_read_bps == sum(member.read_bps)` within 0.1% |

Regenerate with `PYTHONPATH=. uv run python tests/fixtures/e2e/build_fixtures.py`. The script writes every fixture's `dump.vcd` + `axi-bundles.yaml` + `axi-perf.json.golden` from scratch — review the golden diff carefully before committing.

## Runtime budget — measured

Measured on the trust-set fixtures (development machine; Darwin arm64, Python 3.13, pywellen 0.20.5). Each measurement is end-to-end `ingest → reconstruct → aggregate → emit` via the harness's `_write_golden` path:

| Fixture | Posedges | Txns | VCD size | Wall time |
|---------|----------|------|----------|-----------|
| `errors/` | 240 | 12 | ~6 KB | <50 ms |
| `single_master_single_slave/` | 2,034 | 200 | ~57 KB | <100 ms |
| `out_of_order/` | 110 | 16 | ~9 KB | <50 ms |
| `crossbar_2x2/` | 1,620 | 250 | ~95 KB | <200 ms |

100 MB and 1 GB FST inputs: **not measured here, tighten later.** Wellen's mmap-backed FST reader handles multi-GB traces in principle, but the per-bundle `value_at_time` call rate scales linearly with `posedges × bundles`. A 1 GB FST representing ~10⁸ posedges on a wide bundle set is the realistic ceiling, and a real measurement against a Verilator-produced fixture of that scale is a follow-up. Until then, treat the trust-set table above as a sanity check, not a promise.

## When to use the notebook drill-down

`axi-perf.json` rolls per-bundle stats up to a single document. For *per-transaction* visibility (zooming a specific bundle, brushing a latency band) emit the paired `axi-txns.parquet`:

```
axi-profiler run -f f -t X -i foo.fst -o axi-perf.json --emit-txns-parquet
```

Then load the parquet in the marimo notebook template under the `[notebook]` extra:

```
uv pip install rtl-buddy-axi-profiler[notebook]
AXI_TXNS_PARQUET=axi-txns.parquet uv run marimo edit \
    src/rtl_buddy_axi_profiler/notebook/template.py
```

See [`pipeline.md`](pipeline.md) § "Drill-down notebook" for the cell-by-cell tour.

## References

- [`pipeline.md`](pipeline.md) — full five-stage architecture
- [`axi-perf-schema.md`](axi-perf-schema.md) — the emitted JSON contract
- [`axi-bundles-schema.md`](axi-bundles-schema.md) — manifest format
- `src/rtl_buddy_axi_profiler/stages/ingest/wellen.py` — implementation
- `tests/fixtures/e2e/build_fixtures.py` — fixture regeneration entry point
