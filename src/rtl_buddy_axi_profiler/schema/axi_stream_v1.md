# axi-stream binary format v1.0

Locked in [`rtl-buddy-axi-profiler#1`](https://github.com/rtl-buddy/rtl-buddy-axi-profiler/issues/1). Producer is the generated SV monitor from `axi-profiler gen-monitor` (issue #4); consumer is `stages/ingest/stream.py`.

All integer fields are **little-endian**. Strings are null-padded ASCII.

## File layout

```
+--------------------------+
| File header (32 bytes)   |
+--------------------------+
| Bundle table             |
|   (bundle_n * 64 bytes)  |
+--------------------------+
| Record stream            |
|   (variable, 24 B each)  |
| ...                      |
+--------------------------+
```

## File header (32 bytes)

| Offset | Size | Field | Description |
|---|---|---|---|
| 0x00 | u32 | `magic` | `'AXIS'` (= 0x53495841 in LE) |
| 0x04 | u16 | `version` | `0x0001` |
| 0x06 | u16 | `flags` | Reserved; must be 0 |
| 0x08 | u16 | `bundle_n` | Number of entries in the bundle table |
| 0x0A | u8  | `channel_n` | Always 5 (AR, AW, R, W, B) |
| 0x0B | u8  | `_pad` | Reserved |
| 0x0C | u32 | `time_unit` | Picoseconds per tick (e.g. 1000 = ns ticks) |
| 0x10 | u64 | `start_time` | Absolute sim start time in ticks |
| 0x18 | u64 | `_reserved` | Reserved; must be 0 |

## Bundle table

`bundle_n` entries, 64 bytes each.

| Offset | Size | Field | Description |
|---|---|---|---|
| 0x00 | u16 | `bundle_id` | 0..bundle_n-1 |
| 0x02 | u16 | `parent_id` | 0xFFFF for top-level, else parent's bundle_id |
| 0x04 | u16 | `data_width` | Bits |
| 0x06 | u8  | `id_width` | Bits |
| 0x07 | u8  | `protocol` | 0=AXI4, 1=AXI-Lite, 2=AXI5 (v1 emits AXI4 only) |
| 0x08 | char[56] | `name` | Null-padded bundle name; matches `axi-bundles.yaml` |

## Record stream

Records are 24 bytes each, emitted in time order until EOF.

| Offset | Size | Field | Description |
|---|---|---|---|
| 0x00 | u32 | `t_delta` | Ticks since the previous record. `0xFFFFFFFF` is the long-delta sentinel (see below). |
| 0x04 | u16 | `bundle_id` | Index into the bundle table |
| 0x06 | u8  | `channel` | 0=AR, 1=AW, 2=R, 3=W, 4=B |
| 0x07 | u8  | `event_type` | 0=handshake (V&&R), 1=stall_start, 2=stall_end, 0xFF=long_delta |
| 0x08 | u16 | `txn_id` | AXI ID; 0 for W (AXI4 has no WID) |
| 0x0A | u8  | `resp` | 0=OKAY, 1=EXOKAY, 2=SLVERR, 3=DECERR (R and B only; 0 elsewhere) |
| 0x0B | u8  | `last` | 1 for RLAST/WLAST; 0 otherwise |
| 0x0C | u64 | `addr` | AR/AW only; 0 elsewhere |
| 0x14 | u8  | `len` | AXI burst length (beats - 1); 0 for R/W/B records |
| 0x15 | u8  | `size` | AXI burst size (log2 bytes/beat); 0 for R/W/B records |
| 0x16 | u16 | `_pad` | Reserved |

### Long-delta sentinel

When the timestamp gap exceeds the 32-bit `t_delta` field, emit a 24-byte record with:

- `t_delta = 0xFFFFFFFF`
- `event_type = 0xFF`
- `addr` field repurposed as a u64 carrying the actual tick delta.

All other fields are ignored. The next record's `t_delta` resumes the normal interpretation, measured from this sentinel's timestamp.

## Versioning

The 16-bit `version` field is the format version. Bump on any layout-breaking change; readers reject mismatched versions with a typed error (per the schema-version rule from the overlay framework, rtl-buddy-view#17).

Additive changes (e.g., new `event_type` values) keep the same version and a new sentinel.

## Parsing reference

Pseudocode for the consumer (`stages/ingest/stream.py`):

```python
header = struct.unpack("<IHHHBBIQQ", file.read(32))
assert header[0] == 0x53495841  # 'AXIS'
assert header[1] == 1
bundle_n = header[3]

bundles = []
for _ in range(bundle_n):
    raw = file.read(64)
    bundle_id, parent_id, data_width, id_width, protocol = struct.unpack(
        "<HHHBB", raw[:8]
    )
    name = raw[8:64].split(b"\x00", 1)[0].decode("ascii")
    bundles.append((bundle_id, parent_id, data_width, id_width, protocol, name))

t = header[6]  # start_time
while True:
    raw = file.read(24)
    if not raw:
        break
    t_delta, bundle_id, channel, event_type, txn_id, resp, last, addr, length, size, _ = (
        struct.unpack("<IHBBHBBQBBH", raw)
    )
    if event_type == 0xFF:        # long_delta sentinel
        t += addr
        continue
    t += t_delta
    yield HandshakeEvent(t, bundle_id, channel, ...)
```

## Notes

- Single shared file per simulation. The SV monitor opens the file at sim start, writes the header + bundle table, then streams records as handshakes fire. The file is closed at `$finish`.
- Record buffering inside the monitor is implementation-detail; the on-disk layout is what the consumer reads.
- Compression is **not** specified at v1. Wrap with `gzip` externally if disk space matters.
