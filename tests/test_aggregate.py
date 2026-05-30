"""Tests for the standard aggregate stage.

Synthetic-Transaction fixtures only — no FST/VCD here. The
event-stream-derived metrics (channel util%, bp%, peak_occ) are
deferred until reconstruct lands; this PR exercises the
transaction-derived metrics only.
"""

from __future__ import annotations

import pytest

from rtl_buddy_axi_profiler.stages.aggregate.standard import (
    StandardAggregate,
    _build_log2_hist,
    _jain_fairness,
    _percentile,
    aggregate,
    fill_channel_cycle_metrics,
)
from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    Channel,
    DefaultView,
    Manifest,
    Protocol,
    Transaction,
)


def _bundle(
    name: str = "cpu_to_dram",
    master: str = "soc.u_cpu",
    slave: str = "soc.u_dram",
    data_width: int = 64,
) -> Bundle:
    return Bundle(
        name=name,
        master_path=master,
        slave_path=slave,
        protocol=Protocol.AXI4,
        data_width=data_width,
        id_width=4,
        source=BundleSource.VERIBLE_REGEX,
        default_view=DefaultView.PARENT,
    )


def _read_txn(
    *,
    bundle: str = "cpu_to_dram",
    txn_id: int = 1,
    len_beats: int = 1,
    t_start: int = 0,
    t_first: int = 10,
    t_end: int = 20,
    resp: int = 0,
) -> Transaction:
    return Transaction(
        bundle_name=bundle,
        is_read=True,
        txn_id=txn_id,
        addr=0,
        len_beats=len_beats,
        size_log2=3,
        t_start_fs=t_start,
        t_first_data_fs=t_first,
        t_end_fs=t_end,
        resp=resp,
    )


def _write_txn(
    *,
    bundle: str = "cpu_to_dram",
    txn_id: int = 1,
    len_beats: int = 1,
    t_start: int = 0,
    t_end: int = 20,
    resp: int = 0,
) -> Transaction:
    return Transaction(
        bundle_name=bundle,
        is_read=False,
        txn_id=txn_id,
        addr=0,
        len_beats=len_beats,
        size_log2=3,
        t_start_fs=t_start,
        t_first_data_fs=t_start,
        t_end_fs=t_end,
        resp=resp,
    )


def test_aggregate_empty_manifest_yields_empty_bundles() -> None:
    manifest = Manifest(schema_version="1.0", design_top="top", bundles=())
    stats = aggregate(iter([]), manifest, duration_cycles=0, clock_period_ns=1.0)
    assert stats.bundles == []
    assert stats.interconnects == []


def test_aggregate_counts_reads_and_writes() -> None:
    manifest = Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(_bundle(),),
    )
    # Transaction.len_beats is actual beat count (1..256) since the
    # reconstruct stage applies AxLEN + 1. Build the synthetic txns
    # the same way.
    txns = [
        _read_txn(txn_id=1, len_beats=1),
        _read_txn(txn_id=2, len_beats=4, t_start=10, t_first=15, t_end=30),
        _write_txn(txn_id=1, len_beats=2, t_start=5, t_end=25),
    ]
    stats = aggregate(iter(txns), manifest, duration_cycles=100, clock_period_ns=2.0)
    assert len(stats.bundles) == 1
    bs = stats.bundles[0]
    # 2 read txns, 1 + 4 read beats = 5 total
    assert bs.bundle.name == "cpu_to_dram"
    # 1 write txn, 2 write beats
    # Latency reservoir captured the two read latencies (t_first - t_start)
    assert bs.ar_to_r_first.max == 10 or bs.ar_to_r_first.max == 5
    # Throughput is computed from total bytes / elapsed time
    assert bs.read_bps > 0
    assert bs.write_bps > 0


def test_throughput_is_bits_per_second_not_bytes() -> None:
    """Regression: ``read_bps`` / ``write_bps`` are bits per second.

    The field name (\"bps\") and the v1 axi-perf.json schema both say
    bits/sec. The implementation was missing the ×8 byte→bit
    conversion. Lock both reads and writes to a known value.
    """
    manifest = Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(_bundle(data_width=32),),
    )
    # Single 4-beat read at t=0 → end=1e9 fs = 1ms; 4 beats × 4 B/beat
    # = 16 B; bits/sec = 16 × 8 / 1e-6 = 128e6 bps. (elapsed is in
    # ns-scale here because aggregate uses max(t_end) / 1e15 as the
    # second-domain elapsed time. 1e9 fs == 1e-6 s.)
    txns = [
        _read_txn(txn_id=1, len_beats=4, t_start=0, t_first=10, t_end=1_000_000_000),
    ]
    stats = aggregate(iter(txns), manifest, duration_cycles=100, clock_period_ns=2.0)
    bs = stats.bundles[0]
    # 4 beats × 4 bytes/beat × 8 bits/byte ÷ 1e-6 s = 128_000_000 bps.
    # If anyone drops the ×8 again this assertion breaks.
    assert bs.read_bps == pytest.approx(128_000_000, rel=1e-9)
    # Same arithmetic in reverse for writes.
    write_txns = [
        _write_txn(txn_id=2, len_beats=8, t_start=0, t_end=2_000_000_000),
    ]
    stats_w = aggregate(
        iter(write_txns), manifest, duration_cycles=100, clock_period_ns=2.0
    )
    bs_w = stats_w.bundles[0]
    # 8 beats × 4 bytes/beat × 8 bits/byte ÷ 2e-6 s = 128_000_000 bps.
    assert bs_w.write_bps == pytest.approx(128_000_000, rel=1e-9)


def test_aggregate_counts_errors() -> None:
    manifest = Manifest(schema_version="1.0", design_top="soc", bundles=(_bundle(),))
    txns = [
        _read_txn(resp=2),  # SLVERR
        _read_txn(resp=3),  # DECERR
        _read_txn(resp=0),  # OKAY
    ]
    stats = aggregate(iter(txns), manifest, duration_cycles=0, clock_period_ns=1.0)
    bs = stats.bundles[0]
    assert bs.slverr == 1
    assert bs.decerr == 1


def test_interconnect_rollup_requires_two_masters() -> None:
    """A roll-up only fires when ≥2 distinct masters share the same slave."""
    manifest = Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(
            _bundle(name="cpu_to_dram", master="soc.u_cpu", slave="soc.u_dram"),
            _bundle(name="dma_to_dram", master="soc.u_dma", slave="soc.u_dram"),
        ),
    )
    txns = [
        _read_txn(bundle="cpu_to_dram", t_end=100),
        _write_txn(bundle="dma_to_dram", t_end=100),
    ]
    stats = aggregate(iter(txns), manifest, duration_cycles=100, clock_period_ns=1.0)
    assert len(stats.interconnects) == 1
    ic = stats.interconnects[0]
    assert ic.node_path == "soc.u_dram"
    assert ic.hottest_slave == "soc.u_dram"
    assert ic.hottest_master in ("soc.u_cpu", "soc.u_dma")
    assert 0.0 <= ic.fairness_jain <= 1.0


def test_interconnect_rollup_skips_lone_master() -> None:
    """A slave with only one master upstream isn't an interconnect."""
    manifest = Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(_bundle(),),
    )
    txns = [_read_txn()]
    stats = aggregate(iter(txns), manifest, duration_cycles=10, clock_period_ns=1.0)
    assert stats.interconnects == []


def test_unknown_bundle_in_stream_is_silently_dropped() -> None:
    """Transactions for bundles not in the manifest are ignored."""
    manifest = Manifest(schema_version="1.0", design_top="soc", bundles=(_bundle(),))
    txns = [_read_txn(bundle="ghost"), _read_txn(bundle="cpu_to_dram")]
    stats = aggregate(iter(txns), manifest, duration_cycles=10, clock_period_ns=1.0)
    bs = stats.bundles[0]
    # Only one of the two reads should be counted.
    assert bs.read_bps > 0


def test_percentile_is_monotonic() -> None:
    samples = sorted([1, 5, 10, 20, 50, 100, 200, 500])
    p50 = _percentile(samples, 0.50)
    p95 = _percentile(samples, 0.95)
    p99 = _percentile(samples, 0.99)
    assert p50 <= p95 <= p99
    # 8 samples, idx = 0.50 * 7 = 3.5 → interp between samples[3]=20 and samples[4]=50.
    assert p50 == 35


def test_log2_hist_shape_and_count() -> None:
    samples = [1, 2, 3, 4, 5, 100, 1000, 999_999]
    hist = _build_log2_hist(samples)
    assert len(hist) == 16
    assert sum(hist) == len(samples)
    # 1 → bucket 0, 2-3 → bucket 1, 4-5 → bucket 2
    assert hist[0] == 1
    assert hist[1] == 2
    assert hist[2] == 2


def test_jain_fairness_equal_shares_is_one() -> None:
    assert _jain_fairness([5.0, 5.0, 5.0]) == 1.0


def test_jain_fairness_one_master_dominant_drops() -> None:
    fair = _jain_fairness([100.0, 1.0, 1.0])
    assert 0.0 < fair < 1.0


def test_standard_aggregate_wrapper_matches_direct_call() -> None:
    manifest = Manifest(schema_version="1.0", design_top="soc", bundles=(_bundle(),))
    txns = [_read_txn(), _write_txn()]
    direct = aggregate(iter(txns), manifest, duration_cycles=100, clock_period_ns=2.0)
    wrapped = StandardAggregate(duration_cycles=100, clock_period_ns=2.0).run(
        iter([_read_txn(), _write_txn()]), manifest
    )
    assert wrapped.duration_cycles == direct.duration_cycles
    assert len(wrapped.bundles) == len(direct.bundles)


def test_fill_channel_cycle_metrics_computes_bp_and_util() -> None:
    """The CLI/e2e fill folds ingest per-cycle valid/ready tallies into
    ChannelStats: bp% = stalled/asserted, util% = handshakes/duration,
    and AR/AW/B carry txns while R/W carry beats."""
    manifest = Manifest(
        schema_version="1.0", design_top="top", bundles=(_bundle(name="b"),)
    )
    stats = aggregate(iter([]), manifest, duration_cycles=1000, clock_period_ns=1.0)
    # Synthetic ingest tallies: W stalled 90 of 100 asserted cycles;
    # AR fully un-stalled.
    acc = {
        "b": {
            "w": {"active": 100, "stall": 90, "handshakes": 10},
            "ar": {"active": 10, "stall": 0, "handshakes": 10},
        }
    }
    fill_channel_cycle_metrics(stats, acc, 1000)
    ch = stats.bundles[0].channels
    assert ch[Channel.W].bp_pct == 90.0  # 90 / 100
    assert ch[Channel.W].util_pct == 1.0  # 10 / 1000
    assert ch[Channel.W].beats == 10 and ch[Channel.W].txns == 0  # W → beats
    assert ch[Channel.AR].bp_pct == 0.0
    assert ch[Channel.AR].txns == 10 and ch[Channel.AR].beats == 0  # AR → txns
    # Channels with no tally stay zeroed.
    assert ch[Channel.B].bp_pct == 0.0 and ch[Channel.B].txns == 0


def test_fill_channel_cycle_metrics_empty_acc_is_noop() -> None:
    manifest = Manifest(
        schema_version="1.0", design_top="top", bundles=(_bundle(name="b"),)
    )
    stats = aggregate(iter([]), manifest, duration_cycles=1000, clock_period_ns=1.0)
    fill_channel_cycle_metrics(stats, {}, 1000)
    for cs in stats.bundles[0].channels.values():
        assert cs.bp_pct == 0.0 and cs.util_pct == 0.0
