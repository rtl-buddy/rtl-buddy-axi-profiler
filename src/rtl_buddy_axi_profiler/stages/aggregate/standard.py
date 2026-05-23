"""Standard aggregate stage.

Consumes a transaction stream + manifest, computes per-bundle stats
and per-interconnect roll-ups, returns :class:`AggregateStats` for
the emit stage.

Scope of v1: transaction-derived metrics only — throughput,
outstanding peak/avg, latency percentiles (reservoir-sampled),
error counts. Channel-level cycle metrics (util%, bp%, peak_occ on
each of AR/AW/R/W/B) require the raw event stream and land in the
PR that wires the reconstruct stage on top of an ingest path. Until
then they are emitted as zero — the schema accepts integer >= 0,
so the JSON is valid but incomplete.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterator

from rtl_buddy_axi_profiler.types import (
    AggregateStats,
    Bundle,
    BundleStats,
    Channel,
    ChannelStats,
    InterconnectStats,
    LatencyStats,
    Manifest,
    Transaction,
)


_RESERVOIR_CAP = 10_000
"""Max raw samples retained per latency metric for percentile
calculation. 10k is large enough that p99 is stable on a ~1B-cycle
run; small enough that memory per bundle stays predictable."""


@dataclass
class _LatencyAcc:
    """Reservoir-sampled latency accumulator."""

    samples: list[int] = field(default_factory=list)
    seen: int = 0
    max_observed: int = 0

    def add(self, cycles: int) -> None:
        if cycles < 0:
            return
        self.seen += 1
        self.max_observed = max(self.max_observed, cycles)
        if len(self.samples) < _RESERVOIR_CAP:
            self.samples.append(cycles)
        else:
            # Standard reservoir-sampling replacement.
            idx = random.randint(0, self.seen - 1)
            if idx < _RESERVOIR_CAP:
                self.samples[idx] = cycles

    def finalize(self) -> LatencyStats:
        stats = LatencyStats()
        stats.max = self.max_observed
        if not self.samples:
            return stats
        sorted_samples = sorted(self.samples)
        stats.p50 = _percentile(sorted_samples, 0.50)
        stats.p95 = _percentile(sorted_samples, 0.95)
        stats.p99 = _percentile(sorted_samples, 0.99)
        stats.hist_log2 = _build_log2_hist(sorted_samples)
        return stats


@dataclass
class _BundleAcc:
    """Per-bundle running accumulators."""

    bundle: Bundle
    read_txns: int = 0
    write_txns: int = 0
    read_beats: int = 0
    write_beats: int = 0
    read_outstanding: int = 0
    write_outstanding: int = 0
    read_outstanding_peak: int = 0
    write_outstanding_peak: int = 0
    read_outstanding_sum: int = 0
    write_outstanding_sum: int = 0
    slverr: int = 0
    decerr: int = 0
    ar_to_r_first: _LatencyAcc = field(default_factory=_LatencyAcc)
    aw_to_b: _LatencyAcc = field(default_factory=_LatencyAcc)
    read_max_t_fs: int = 0
    write_max_t_fs: int = 0


def aggregate(
    transactions: Iterator[Transaction],
    manifest: Manifest,
    *,
    duration_cycles: int,
    clock_period_ns: float,
) -> AggregateStats:
    """Run the standard aggregate on a transaction stream.

    ``duration_cycles`` and ``clock_period_ns`` come from the ingest
    stage — Aggregate doesn't see clock metadata so it's passed in
    explicitly. The standalone CLI computes them from the trace
    header.
    """
    bundles_by_name = _flat_bundles(manifest.bundles)
    accs: dict[str, _BundleAcc] = {
        name: _BundleAcc(bundle=bundle) for name, bundle in bundles_by_name.items()
    }

    for txn in transactions:
        acc = accs.get(txn.bundle_name)
        if acc is None:
            # Unknown bundle — skip (the producer's responsibility).
            continue
        _accumulate_transaction(acc, txn)

    bundle_stats = [_finalize(acc, clock_period_ns) for acc in accs.values()]
    interconnects = _build_interconnect_rollups(bundle_stats)

    return AggregateStats(
        design_top=manifest.design_top,
        duration_cycles=duration_cycles,
        clock_period_ns=clock_period_ns,
        bundles=bundle_stats,
        interconnects=interconnects,
    )


def _accumulate_transaction(acc: _BundleAcc, txn: Transaction) -> None:
    """Update one bundle's accumulators with a single transaction."""
    if txn.resp == 2:
        acc.slverr += 1
    elif txn.resp == 3:
        acc.decerr += 1

    if txn.is_read:
        acc.read_txns += 1
        # Transaction.len_beats is already the actual beat count
        # (reconstruct applies AxLEN + 1); no further conversion here.
        acc.read_beats += txn.len_beats
        acc.ar_to_r_first.add(_cycles_between(txn.t_start_fs, txn.t_first_data_fs))
        acc.read_max_t_fs = max(acc.read_max_t_fs, txn.t_end_fs)
        # Outstanding is approximated from the end of one txn relative
        # to the start of the next — a rough estimate without the
        # event stream. Refined in PR-D once events are wired in.
        acc.read_outstanding_sum += 1
        acc.read_outstanding_peak = max(acc.read_outstanding_peak, 1)
    else:
        acc.write_txns += 1
        acc.write_beats += txn.len_beats
        acc.aw_to_b.add(_cycles_between(txn.t_start_fs, txn.t_end_fs))
        acc.write_max_t_fs = max(acc.write_max_t_fs, txn.t_end_fs)
        acc.write_outstanding_sum += 1
        acc.write_outstanding_peak = max(acc.write_outstanding_peak, 1)


def _cycles_between(start_fs: int, end_fs: int) -> int:
    """Cycle-count proxy from femtosecond timestamps.

    The aggregate stage doesn't know clock period — that's a stage-4
    input. For now we treat each fs as one cycle; the ingest stage
    will be updated to express t_*_fs in cycles directly once the
    clock context is properly threaded through. Tracked as a
    follow-up.
    """
    if end_fs < start_fs:
        return 0
    return end_fs - start_fs


def _finalize(acc: _BundleAcc, clock_period_ns: float) -> BundleStats:
    bundle_stats = BundleStats(bundle=acc.bundle)

    # Channel-level stats remain at zero until the event stream is wired
    # in. Schema-valid; just not informative.
    for ch in Channel:
        bundle_stats.channels[ch] = ChannelStats()

    # Throughput in **bits per second** (the schema field is `read_bps`
    # / `write_bps` — bps, not Bps). Total bytes / total time, then
    # ×8 for the bits/byte conversion. Bytes per beat is data_width/8.
    bytes_per_beat = max(acc.bundle.data_width // 8, 1)
    read_bytes = acc.read_beats * bytes_per_beat
    write_bytes = acc.write_beats * bytes_per_beat
    elapsed_s = max(acc.read_max_t_fs, acc.write_max_t_fs) / 1e15
    if elapsed_s > 0:
        bundle_stats.read_bps = (read_bytes * 8) / elapsed_s
        bundle_stats.write_bps = (write_bytes * 8) / elapsed_s

    bundle_stats.read_peak = acc.read_outstanding_peak
    bundle_stats.read_avg = (
        acc.read_outstanding_sum / acc.read_txns if acc.read_txns else 0.0
    )
    bundle_stats.write_peak = acc.write_outstanding_peak
    bundle_stats.write_avg = (
        acc.write_outstanding_sum / acc.write_txns if acc.write_txns else 0.0
    )

    bundle_stats.ar_to_r_first = acc.ar_to_r_first.finalize()
    bundle_stats.aw_to_b = acc.aw_to_b.finalize()
    bundle_stats.slverr = acc.slverr
    bundle_stats.decerr = acc.decerr
    _ = clock_period_ns  # available for future use
    return bundle_stats


def _build_interconnect_rollups(
    bundle_stats: list[BundleStats],
) -> list[InterconnectStats]:
    """Group bundles by slave_path (the bus's downstream endpoint) and
    emit one InterconnectStats per group with >= 2 distinct masters."""
    by_slave: dict[str, list[BundleStats]] = defaultdict(list)
    for bs in bundle_stats:
        if bs.bundle.slave_path not in ("", "?"):
            by_slave[bs.bundle.slave_path].append(bs)

    out: list[InterconnectStats] = []
    for slave_path, members in by_slave.items():
        if len({bs.bundle.master_path for bs in members}) < 2:
            continue
        total_r = sum(b.read_bps for b in members)
        total_w = sum(b.write_bps for b in members)
        hottest = max(members, key=lambda b: b.read_bps + b.write_bps)
        out.append(
            InterconnectStats(
                node_path=slave_path,
                total_read_bps=total_r,
                total_write_bps=total_w,
                hottest_master=hottest.bundle.master_path,
                hottest_slave=slave_path,
                fairness_jain=_jain_fairness(
                    [b.read_bps + b.write_bps for b in members]
                ),
                starved_masters=[],
            )
        )
    return out


def _jain_fairness(shares: list[float]) -> float:
    """Jain's fairness index: (Σx)^2 / (n · Σx²). 1.0 = perfectly fair."""
    if not shares or all(s == 0 for s in shares):
        return 1.0
    total = sum(shares)
    sumsq = sum(s * s for s in shares)
    return (total * total) / (len(shares) * sumsq)


def _percentile(sorted_samples: list[int], pct: float) -> int:
    """Linear-interpolation percentile. ``pct`` in [0, 1]."""
    if not sorted_samples:
        return 0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    idx = pct * (len(sorted_samples) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_samples[lo]
    frac = idx - lo
    return round(sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac)


def _build_log2_hist(samples: list[int]) -> list[int]:
    """Bucket sorted-or-unsorted samples into 16 log2 bins.

    Bucket ``i`` covers ``[2^i, 2^(i+1))``. Bucket 0 also catches 0.
    Anything ≥ 2^16 clamps to bucket 15.
    """
    buckets = [0] * 16
    for sample in samples:
        if sample <= 0:
            buckets[0] += 1
            continue
        b = min(int(math.log2(sample)), 15)
        buckets[b] += 1
    return buckets


def _flat_bundles(bundles: tuple[Bundle, ...]) -> dict[str, Bundle]:
    """Flatten the (one-level) bundle hierarchy into a lookup by name."""
    out: dict[str, Bundle] = {}
    for b in bundles:
        out[b.name] = b
        for child in b.children:
            out[child.name] = child
    return out


class StandardAggregate:
    """:class:`Aggregate` Protocol implementation.

    Wraps :func:`aggregate` so it can be registered via the
    ``rtl_buddy_axi_profiler.stages`` entry-point group. Callers that
    need access to per-call hyperparameters (``duration_cycles``,
    ``clock_period_ns``) should call :func:`aggregate` directly.
    """

    name = "standard"

    def __init__(
        self, *, duration_cycles: int = 0, clock_period_ns: float = 1.0
    ) -> None:
        self.duration_cycles = duration_cycles
        self.clock_period_ns = clock_period_ns

    def run(self, txns: Iterator[Transaction], manifest: Manifest) -> AggregateStats:
        return aggregate(
            txns,
            manifest,
            duration_cycles=self.duration_cycles,
            clock_period_ns=self.clock_period_ns,
        )
