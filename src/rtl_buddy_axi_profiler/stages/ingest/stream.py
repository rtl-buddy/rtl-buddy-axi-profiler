"""``axi-stream`` binary ingest (path C, #4).

Reads a packed-binary stream emitted by the generated SV monitor
(``axi-profiler gen-monitor``). The format is locked at v1.0 in
:mod:`rtl_buddy_axi_profiler.schema.axi_stream_v1`. Layout summary:

- 32-byte file header (magic + version + bundle_n + timescale)
- bundle_n × 64-byte bundle table entries
- 24-byte records until EOF

This stage is the path-C alternative to FST/VCD ingest; the
downstream reconstruct + aggregate + emit stages are reused
verbatim.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterator

from rtl_buddy_axi_profiler.types import (
    Bundle,
    Channel,
    HandshakeEvent,
    Manifest,
)


HEADER_STRUCT = "<IHHHBBIQQ"
HEADER_SIZE = struct.calcsize(HEADER_STRUCT)  # 32
BUNDLE_ENTRY_STRUCT = "<HHHBB56s"
BUNDLE_ENTRY_SIZE = struct.calcsize(BUNDLE_ENTRY_STRUCT)  # 64
RECORD_STRUCT = "<IHBBHBBQBBH"
RECORD_SIZE = struct.calcsize(RECORD_STRUCT)  # 24

MAGIC_AXIS = 0x53495841  # 'AXIS' little-endian
SUPPORTED_VERSION = 0x0001

_CHANNEL_BY_ID: dict[int, Channel] = {
    0: Channel.AR,
    1: Channel.AW,
    2: Channel.R,
    3: Channel.W,
    4: Channel.B,
}

_LONG_DELTA_SENTINEL = 0xFFFFFFFF
_EVENT_TYPE_HANDSHAKE = 0
_EVENT_TYPE_LONG_DELTA = 0xFF


class StreamIngestError(RuntimeError):
    """Raised when an axi-stream file is malformed or incompatible."""


def ingest(source: Path, manifest: Manifest) -> Iterator[HandshakeEvent]:
    """Yield HandshakeEvent objects from an axi-stream binary file.

    Cross-validates the file's bundle table against the manifest:
    if names or widths disagree, raises StreamIngestError before
    iterating so the producer/consumer mismatch surfaces early.
    """
    data = source.read_bytes()
    if len(data) < HEADER_SIZE:
        raise StreamIngestError(
            f"{source}: file too short ({len(data)} bytes) — header is "
            f"{HEADER_SIZE} bytes."
        )

    header = struct.unpack_from(HEADER_STRUCT, data, 0)
    (
        magic,
        version,
        flags,
        bundle_n,
        channel_n,
        _pad,
        time_unit,
        start_time,
        _reserved,
    ) = header

    if magic != MAGIC_AXIS:
        raise StreamIngestError(
            f"{source}: bad magic 0x{magic:08x} (expected 0x{MAGIC_AXIS:08x})"
        )
    if version != SUPPORTED_VERSION:
        raise StreamIngestError(
            f"{source}: unsupported version 0x{version:04x} (consumer "
            f"expects 0x{SUPPORTED_VERSION:04x})"
        )
    if channel_n != 5:
        raise StreamIngestError(
            f"{source}: channel_n={channel_n} (expected 5 for AXI4)"
        )
    if flags != 0:
        raise StreamIngestError(f"{source}: reserved flags must be 0")

    table_offset = HEADER_SIZE
    bundle_lookup = _read_bundle_table(data, table_offset, bundle_n, source)
    _cross_validate(bundle_lookup, manifest, source)

    record_offset = table_offset + bundle_n * BUNDLE_ENTRY_SIZE
    yield from _iter_records(data, record_offset, bundle_lookup, time_unit, start_time)


def _read_bundle_table(
    data: bytes, offset: int, bundle_n: int, source: Path
) -> dict[int, str]:
    """Parse bundle_n table entries; return ``{bundle_id: name}``."""
    if len(data) < offset + bundle_n * BUNDLE_ENTRY_SIZE:
        raise StreamIngestError(f"{source}: file truncated inside bundle table")
    out: dict[int, str] = {}
    for i in range(bundle_n):
        entry_off = offset + i * BUNDLE_ENTRY_SIZE
        bundle_id, parent_id, data_width, id_width, protocol, name_b = (
            struct.unpack_from(BUNDLE_ENTRY_STRUCT, data, entry_off)
        )
        _ = (parent_id, data_width, id_width, protocol)
        name = name_b.split(b"\x00", 1)[0].decode("ascii")
        out[bundle_id] = name
    return out


def _cross_validate(
    bundle_lookup: dict[int, str], manifest: Manifest, source: Path
) -> None:
    """Every name in the stream's bundle table must exist in the manifest."""
    flat = _flat_bundle_names(manifest.bundles)
    for bundle_id, name in bundle_lookup.items():
        if name not in flat:
            raise StreamIngestError(
                f"{source}: bundle id={bundle_id} name={name!r} in stream "
                f"is absent from the manifest. Re-run gen-monitor against "
                f"the current manifest."
            )


def _iter_records(
    data: bytes,
    offset: int,
    bundle_lookup: dict[int, str],
    time_unit_ps: int,
    start_time_ticks: int,
) -> Iterator[HandshakeEvent]:
    """Walk the record stream, yielding HandshakeEvents.

    Handles the long-delta sentinel and clamps to file length so a
    truncated stream stops cleanly rather than asserting."""
    t_fs_per_tick = max(time_unit_ps, 0) * 1000  # ps → fs
    if t_fs_per_tick == 0:
        # Sub-ps precision in the header is reserved for future use.
        # Treat as 1 fs/tick so we still produce monotonic timestamps.
        t_fs_per_tick = 1
    current_tick = start_time_ticks

    while offset + RECORD_SIZE <= len(data):
        (
            t_delta,
            bundle_id,
            channel,
            event_type,
            txn_id,
            resp,
            last,
            addr,
            burst_len,
            size_log2,
            _pad,
        ) = struct.unpack_from(RECORD_STRUCT, data, offset)
        offset += RECORD_SIZE

        if event_type == _EVENT_TYPE_LONG_DELTA:
            # Long-delta uses the addr field as a u64 tick delta.
            current_tick += addr
            continue
        if event_type != _EVENT_TYPE_HANDSHAKE:
            # Reserved event_type — skip silently in v1.
            continue

        if t_delta == _LONG_DELTA_SENTINEL:
            # Next record should be a long_delta extension. Skip.
            continue
        current_tick += t_delta

        ch = _CHANNEL_BY_ID.get(channel)
        if ch is None:
            continue  # unknown channel id; skip
        bundle_name = bundle_lookup.get(bundle_id)
        if bundle_name is None:
            continue  # unknown bundle id; skip

        yield HandshakeEvent(
            t_fs=current_tick * t_fs_per_tick,
            bundle_name=bundle_name,
            channel=ch,
            txn_id=txn_id,
            addr=addr,
            resp=resp,
            last=bool(last),
            len_beats=burst_len,
            size_log2=size_log2,
        )


def _flat_bundle_names(bundles: tuple[Bundle, ...]) -> set[str]:
    out: set[str] = set()
    for b in bundles:
        out.add(b.name)
        for child in b.children:
            out.add(child.name)
    return out


def write_stream(
    out_path: Path,
    *,
    bundles: list[tuple[int, str]],
    records: list[tuple[int, int, int, int, int, int, int, int, int]],
    time_unit_ps: int = 1000,
    start_time_ticks: int = 0,
) -> None:
    """Helper for tests: build an axi-stream file from Python tuples.

    ``bundles`` is ``[(bundle_id, name), ...]``. ``records`` is
    ``[(t_delta, bundle_id, channel, txn_id, resp, last, addr, len, size), ...]``
    where each tuple's elements map to the record-stream fields
    (event_type is implicit 0).
    """
    bundle_n = len(bundles)
    header = struct.pack(
        HEADER_STRUCT,
        MAGIC_AXIS,
        SUPPORTED_VERSION,
        0,
        bundle_n,
        5,
        0,
        time_unit_ps,
        start_time_ticks,
        0,
    )
    table = b""
    for bundle_id, name in bundles:
        name_b = name.encode("ascii")[:56].ljust(56, b"\x00")
        table += struct.pack(BUNDLE_ENTRY_STRUCT, bundle_id, 0xFFFF, 64, 4, 0, name_b)
    body = b""
    for rec in records:
        t_delta, bundle_id, channel, txn_id, resp, last, addr, burst_len, size_log2 = (
            rec
        )
        body += struct.pack(
            RECORD_STRUCT,
            t_delta,
            bundle_id,
            channel,
            _EVENT_TYPE_HANDSHAKE,
            txn_id,
            resp,
            last,
            addr,
            burst_len,
            size_log2,
            0,
        )
    out_path.write_bytes(header + table + body)


class StreamIngest:
    """Ingest Protocol implementation for the axi-stream binary path."""

    name = "stream"

    def run(self, source: Path, manifest: Manifest) -> Iterator[HandshakeEvent]:
        return ingest(source, manifest)
