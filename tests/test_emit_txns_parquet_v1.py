"""Tests for the txns_parquet_v1 emit stage.

Round-trip synthetic Transaction lists through emit + pyarrow read,
plus the file-level metadata, the empty-input case, and the
``pyarrow-not-installed`` error path.

The happy-path tests skip when pyarrow isn't available so dev
environments without the ``[parquet]`` extra still pass. CI installs
the extra explicitly (`uv sync --group test --extra parquet`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rtl_buddy_axi_profiler.stages.emit.txns_parquet_v1 import (
    SCHEMA_VERSION,
    TxnsParquetError,
    emit_txns_parquet,
)
from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    DefaultView,
    Manifest,
    Protocol,
    Transaction,
)


pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


def _make_manifest() -> Manifest:
    bundle = Bundle(
        name="cpu_to_xbar",
        master_path="soc.u_cpu",
        slave_path="soc.u_xbar",
        protocol=Protocol.AXI4,
        data_width=64,
        id_width=4,
        source=BundleSource.USER,
        default_view=DefaultView.PARENT,
    )
    return Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(bundle,),
        generated_by="test",
        generated_at="2026-05-22T08:00:00Z",
    )


def _read_txn() -> Transaction:
    return Transaction(
        bundle_name="cpu_to_xbar",
        is_read=True,
        txn_id=7,
        addr=0x1000,
        len_beats=4,
        size_log2=3,
        t_start_fs=1_000_000,
        t_first_data_fs=3_000_000,  # 2_000_000 fs after start = 1 cycle at 2 ns
        t_end_fs=11_000_000,
        resp=0,
    )


def _write_txn() -> Transaction:
    return Transaction(
        bundle_name="cpu_to_xbar",
        is_read=False,
        txn_id=12,
        addr=0x2000,
        len_beats=2,
        size_log2=3,
        t_start_fs=20_000_000,
        t_first_data_fs=20_000_000,  # writes mirror t_aw_fs here
        t_end_fs=26_000_000,  # 6_000_000 fs after start = 3 cycles at 2 ns
        resp=2,  # SLVERR
    )


def test_roundtrip_read_and_write_rows(tmp_path: Path) -> None:
    out = tmp_path / "axi-txns.parquet"
    emit_txns_parquet(
        [_read_txn(), _write_txn()],
        _make_manifest(),
        out,
        clock_period_ns=2.0,
    )
    table = pq.read_table(out)
    assert table.num_rows == 2

    by_id = {row["txn_id"]: row for row in table.to_pylist()}

    read_row = by_id[7]
    assert read_row["bundle_name"] == "cpu_to_xbar"
    assert read_row["is_read"] is True
    assert read_row["addr"] == 0x1000
    assert read_row["len_beats"] == 4
    assert read_row["size_log2"] == 3
    # v1.1 schema: ps = fs // 1000. Inputs are 1e6/3e6/11e6 fs → 1e3/3e3/11e3 ps.
    assert read_row["t_start_ps"] == 1_000
    assert read_row["t_first_data_ps"] == 3_000
    assert read_row["t_end_ps"] == 11_000
    assert read_row["resp"] == 0
    assert read_row["ar_to_r_first_cyc"] == 1  # (3e6 - 1e6) fs / 2e6 fs/cyc
    assert read_row["aw_to_b_cyc"] is None
    assert read_row["master_path"] == "soc.u_cpu"
    assert read_row["slave_path"] == "soc.u_xbar"

    write_row = by_id[12]
    assert write_row["is_read"] is False
    assert write_row["t_first_data_ps"] is None
    assert write_row["ar_to_r_first_cyc"] is None
    assert write_row["aw_to_b_cyc"] == 3  # (26 - 20) fs / 2 fs/cyc... in 1e6 units
    assert write_row["resp"] == 2


def test_arrow_schema_types_and_nullability(tmp_path: Path) -> None:
    """Locks the parquet wire shape — downstream consumers (marimo
    template, hub-emitted views) build against these exact types."""
    out = tmp_path / "axi-txns.parquet"
    emit_txns_parquet([_read_txn()], _make_manifest(), out, clock_period_ns=2.0)
    schema = pq.read_schema(out)
    by_name = {f.name: f for f in schema}

    assert by_name["bundle_name"].type == pa.string()
    assert by_name["is_read"].type == pa.bool_()
    assert by_name["txn_id"].type == pa.int64()
    assert by_name["addr"].type == pa.int64()
    assert by_name["len_beats"].type == pa.int32()
    assert by_name["size_log2"].type == pa.int32()
    assert by_name["t_start_ps"].type == pa.int64()
    assert by_name["t_first_data_ps"].type == pa.int64()
    assert by_name["t_end_ps"].type == pa.int64()
    assert by_name["resp"].type == pa.int8()
    assert by_name["ar_to_r_first_cyc"].type == pa.int64()
    assert by_name["aw_to_b_cyc"].type == pa.int64()
    assert by_name["master_path"].type == pa.string()
    assert by_name["slave_path"].type == pa.string()

    # Nullability: per v1.1 schema.
    assert by_name["t_first_data_ps"].nullable is True
    assert by_name["ar_to_r_first_cyc"].nullable is True
    assert by_name["aw_to_b_cyc"].nullable is True
    assert by_name["bundle_name"].nullable is False
    assert by_name["is_read"].nullable is False
    assert by_name["resp"].nullable is False


def test_file_level_metadata(tmp_path: Path) -> None:
    out = tmp_path / "axi-txns.parquet"
    emit_txns_parquet([_read_txn()], _make_manifest(), out, clock_period_ns=2.0)

    schema_meta = pq.read_schema(out).metadata or {}
    assert schema_meta.get(b"schema_version") == SCHEMA_VERSION.encode()
    assert schema_meta.get(b"design_top") == b"soc"
    assert schema_meta.get(b"clock_period_ns") == b"2.0"
    produced_by = (schema_meta.get(b"produced_by") or b"").decode()
    assert produced_by.startswith("rtl-buddy-axi-profiler v")
    # produced_at present + parseable as the YYYY-MM-DDTHH:MM:SSZ
    # subset of ISO-8601.
    produced_at = (schema_meta.get(b"produced_at") or b"").decode()
    assert produced_at.endswith("Z")
    assert len(produced_at) == len("2026-05-22T08:00:00Z")


def test_empty_transactions_yields_zero_row_file(tmp_path: Path) -> None:
    """A test that fires zero transactions still produces a valid
    parquet — the notebook should never error-out on a clean run."""
    out = tmp_path / "axi-txns.parquet"
    emit_txns_parquet([], _make_manifest(), out, clock_period_ns=2.0)
    table = pq.read_table(out)
    assert table.num_rows == 0
    assert set(table.column_names) == {
        "bundle_name",
        "is_read",
        "txn_id",
        "addr",
        "len_beats",
        "size_log2",
        "t_start_ps",
        "t_first_data_ps",
        "t_end_ps",
        "resp",
        "ar_to_r_first_cyc",
        "aw_to_b_cyc",
        "master_path",
        "slave_path",
    }


def test_read_with_no_r_beats_yields_null_first_data(tmp_path: Path) -> None:
    """A read txn that never saw an R beat (sentinel
    ``Transaction.t_first_data_fs == 0``) emits null for both
    first-data ps and the derived cycle latency."""
    incomplete_read = Transaction(
        bundle_name="cpu_to_xbar",
        is_read=True,
        txn_id=99,
        addr=0x3000,
        len_beats=1,
        size_log2=3,
        t_start_fs=5_000_000,
        t_first_data_fs=0,
        t_end_fs=5_000_000,
        resp=0,
    )
    out = tmp_path / "axi-txns.parquet"
    emit_txns_parquet([incomplete_read], _make_manifest(), out, clock_period_ns=2.0)
    row = pq.read_table(out).to_pylist()[0]
    assert row["t_first_data_ps"] is None
    assert row["ar_to_r_first_cyc"] is None


def test_pyarrow_missing_raises_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the user-visible error so the CLI's exit-code-2 mapping
    keeps surfacing a useful install hint, not a bare ImportError."""
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    with pytest.raises(TxnsParquetError) as exc_info:
        emit_txns_parquet(
            [_read_txn()],
            _make_manifest(),
            tmp_path / "axi-txns.parquet",
            clock_period_ns=2.0,
        )
    assert "pyarrow" in str(exc_info.value)
    assert "[parquet]" in str(exc_info.value)
