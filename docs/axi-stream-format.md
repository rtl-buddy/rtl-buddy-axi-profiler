# axi-stream binary format v1.0

The packed-binary intermediate emitted by the generated SV monitor (`axi-profiler gen-monitor`, issue #4) and consumed by `StreamIngest`. This format exists so long-run sims that would dump tens of gigabytes of FST instead emit ~24 bytes per AXI handshake.

The formal layout reference is [`src/rtl_buddy_axi_profiler/schema/axi_stream_v1.md`](../src/rtl_buddy_axi_profiler/schema/axi_stream_v1.md) (machine-checked size constants live in [`tests/test_schema_validation.py`](../tests/test_schema_validation.py)). This doc is the human-readable companion.

## Why a custom format

For a representative SoC simulation:

| Format         | Bytes per cycle (typical AXI bundle) | 10 Mcycle run (~100k handshakes) |
|----------------|--------------------------------------|----------------------------------|
| FST (full hier dump) | ~50 B/cycle                    | ~500 MB                          |
| `axi-stream`   | ~0 B/cycle + 24 B/handshake          | ~2.5 MB                          |

The win is asymmetric on workloads with sparse traffic: the stream only writes when handshakes fire, while FST samples every clock.

## File layout

```
+--------------------------+
| File header (32 bytes)   |
+--------------------------+
| Bundle table             |
|   (bundle_n × 64 bytes)  |
+--------------------------+
| Record stream            |
|   (variable; 24 B each)  |
| ...                      |
+--------------------------+
```

All integers are **little-endian**. Strings are null-padded ASCII.

## Producer / consumer pairing

- Producer: the generated SV monitor module. Each per-bundle sub-module snoops the five AXI channels via signal paths from the manifest, encodes a 24-byte record on every `valid && ready`, and `$fwrite`s to a shared file handle opened by the top wrapper.
- Consumer: `stages/ingest/stream.py`. Memory-mapped read; `struct.unpack` of the record stream; yields `HandshakeEvent` to the reconstruct stage.

Producer and consumer **must** match exactly on layout. Cross-validation: the consumer checks the bundle table against the manifest YAML and errors out on width / name mismatches.

## See also

- [Layout spec](../src/rtl_buddy_axi_profiler/schema/axi_stream_v1.md) — the canonical reference.
- Issue #4 — implementation of `gen-monitor` and `StreamIngest`.
- Issue #3 — the equivalent FST path; goldens shared between the two ingest variants.
