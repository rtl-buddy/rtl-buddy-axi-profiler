"""Tests for the axi-stream binary StreamIngest (#4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_axi_profiler.stages.ingest.stream import (
    StreamIngest,
    StreamIngestError,
    ingest,
    write_stream,
)
from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    Channel,
    DefaultView,
    Manifest,
    Protocol,
)


def _manifest_with(*names: str) -> Manifest:
    bundles = tuple(
        Bundle(
            name=n,
            master_path=f"top.u_master_{i}",
            slave_path=f"top.u_slave_{i}",
            protocol=Protocol.AXI4,
            data_width=64,
            id_width=4,
            source=BundleSource.VERIBLE_REGEX,
            default_view=DefaultView.PARENT,
        )
        for i, n in enumerate(names)
    )
    return Manifest(schema_version="1.0", design_top="top", bundles=bundles)


def test_round_trip_single_handshake(tmp_path: Path) -> None:
    """Write a 1-record stream and read it back."""
    axis = tmp_path / "trace.axis"
    write_stream(
        axis,
        bundles=[(0, "cpu_to_dram")],
        records=[
            # (t_delta, bundle_id, channel=AR(0), txn_id, resp, last, addr, len, size)
            (10, 0, 0, 7, 0, 0, 0x100, 0, 3),
        ],
        time_unit_ps=1_000,  # 1ns/tick
    )
    events = list(ingest(axis, _manifest_with("cpu_to_dram")))
    assert len(events) == 1
    e = events[0]
    assert e.bundle_name == "cpu_to_dram"
    assert e.channel == Channel.AR
    assert e.txn_id == 7
    assert e.addr == 0x100
    # 1 tick at 1ns = 1_000 ps = 1_000_000 fs, delta=10
    assert e.t_fs == 10_000_000


def test_round_trip_multi_channel(tmp_path: Path) -> None:
    axis = tmp_path / "trace.axis"
    write_stream(
        axis,
        bundles=[(0, "b0")],
        records=[
            (5, 0, 0, 1, 0, 0, 0x100, 0, 3),  # AR
            (3, 0, 2, 1, 0, 1, 0, 0, 0),  # R with last
        ],
        time_unit_ps=1_000,
    )
    events = list(ingest(axis, _manifest_with("b0")))
    assert len(events) == 2
    assert events[0].channel == Channel.AR
    assert events[1].channel == Channel.R
    assert events[1].last is True


def test_rejects_wrong_magic(tmp_path: Path) -> None:
    axis = tmp_path / "trace.axis"
    axis.write_bytes(b"\x00" * 64)
    with pytest.raises(StreamIngestError):
        list(ingest(axis, _manifest_with("b0")))


def test_rejects_unknown_version(tmp_path: Path) -> None:
    """A stream with version!=1 should be rejected loudly."""
    import struct
    from rtl_buddy_axi_profiler.stages.ingest.stream import (
        HEADER_STRUCT,
        MAGIC_AXIS,
    )

    axis = tmp_path / "trace.axis"
    bad = struct.pack(HEADER_STRUCT, MAGIC_AXIS, 0xFFFF, 0, 0, 5, 0, 1000, 0, 0)
    axis.write_bytes(bad)
    with pytest.raises(StreamIngestError):
        list(ingest(axis, _manifest_with("b0")))


def test_cross_validation_catches_missing_bundle(tmp_path: Path) -> None:
    """A bundle name in the stream that's absent from the manifest = error."""
    axis = tmp_path / "trace.axis"
    write_stream(
        axis,
        bundles=[(0, "ghost_bundle")],
        records=[],
    )
    with pytest.raises(StreamIngestError):
        list(ingest(axis, _manifest_with("real_bundle")))


def test_truncated_record_stream_stops_cleanly(tmp_path: Path) -> None:
    """A file that ends mid-record (e.g. sim crash) just stops yielding."""
    axis = tmp_path / "trace.axis"
    write_stream(
        axis,
        bundles=[(0, "b0")],
        records=[(5, 0, 0, 1, 0, 0, 0, 0, 0)],
    )
    # Append a few junk bytes — less than RECORD_SIZE.
    data = axis.read_bytes()
    axis.write_bytes(data + b"\x00\x00\x00\x00")
    events = list(ingest(axis, _manifest_with("b0")))
    # Original record still parsed; junk silently dropped.
    assert len(events) == 1


def test_stream_ingest_class_wrapper(tmp_path: Path) -> None:
    axis = tmp_path / "trace.axis"
    write_stream(
        axis,
        bundles=[(0, "b0")],
        records=[(5, 0, 0, 1, 0, 0, 0x200, 0, 0)],
    )
    stage = StreamIngest()
    events = list(stage.run(axis, _manifest_with("b0")))
    assert len(events) == 1
    assert events[0].addr == 0x200
