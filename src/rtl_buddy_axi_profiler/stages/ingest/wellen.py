"""FST/VCD ingest via pywellen.

Opens the waveform with pywellen, autodetects the global clock, and
yields one :class:`HandshakeEvent` per (clock-posedge × bundle ×
channel) cycle where ``valid && ready`` is observed. Event-driven
sampling — we walk the clock's posedge times, not every change on
every AXI signal.

Multi-clock support: deferred. v1 assumes a single global clock for
the AXI fabric; the detector picks the highest-frequency 1-bit
signal in the trace.

Path resolution: signal paths in the manifest must match the trace's
fully-qualified paths. ``tb_prefix`` stripping (rtl-buddy-view #21)
is a follow-up; for v1, regenerate the manifest from inside the
testbench scope if your sim wraps the design.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pywellen

from rtl_buddy_axi_profiler.stages.ingest._clock_detect import (
    ClockDetectError,
    DetectedClock,
    detect_global_clock,
    resolve_bundle_clock,
)
from rtl_buddy_axi_profiler.types import (
    Bundle,
    Channel,
    HandshakeEvent,
    Manifest,
)


@dataclass(frozen=True)
class _BundleSignals:
    """Per-bundle pywellen Signal handles, looked up once at open time."""

    bundle: Bundle
    arvalid: pywellen.Signal
    arready: pywellen.Signal
    araddr: pywellen.Signal | None
    arid: pywellen.Signal | None
    arlen: pywellen.Signal | None
    arsize: pywellen.Signal | None
    awvalid: pywellen.Signal
    awready: pywellen.Signal
    awaddr: pywellen.Signal | None
    awid: pywellen.Signal | None
    awlen: pywellen.Signal | None
    awsize: pywellen.Signal | None
    rvalid: pywellen.Signal
    rready: pywellen.Signal
    rid: pywellen.Signal | None
    rresp: pywellen.Signal | None
    rlast: pywellen.Signal | None
    wvalid: pywellen.Signal
    wready: pywellen.Signal
    wlast: pywellen.Signal | None
    bvalid: pywellen.Signal
    bready: pywellen.Signal
    bid: pywellen.Signal | None
    bresp: pywellen.Signal | None


class WellenIngestError(RuntimeError):
    """Raised when the trace can't be opened or a manifest signal is missing."""


def ingest(
    source: Path,
    manifest: Manifest,
    *,
    tb_prefix: str = "",
) -> Iterator[HandshakeEvent]:
    """Yield HandshakeEvent objects from a waveform file.

    ``tb_prefix`` is prepended to every manifest signal path when
    the direct lookup misses. Typical values: ``"tb.dut"`` /
    ``"tb_<design>.u_dut"`` — whatever the testbench wraps the
    design under. Empty string (the default) means lookups use the
    manifest paths verbatim.

    See :class:`WellenIngest` for the entry-point class wrapper.
    """
    try:
        waveform = pywellen.Waveform(str(source))
    except Exception as e:
        raise WellenIngestError(f"could not open trace {source}: {e}") from None

    bundles = _resolve_bundles(
        waveform, _flat_bundles(manifest.bundles), tb_prefix=tb_prefix
    )
    bundle_clocks = _resolve_bundle_clocks(waveform, bundles, tb_prefix=tb_prefix)
    yield from _emit_events(waveform, bundle_clocks)


def _resolve_bundle_clocks(
    waveform: pywellen.Waveform,
    bundles: list[_BundleSignals],
    *,
    tb_prefix: str = "",
) -> list[tuple[_BundleSignals, DetectedClock]]:
    """Pair each bundle with its clock.

    Per-bundle clock from the manifest's ``clock_signal`` wins;
    falls back to the global autodetected clock for bundles whose
    manifest entry doesn't set one (legacy v1.0 manifests).
    """
    global_clock: DetectedClock | None = None
    out: list[tuple[_BundleSignals, DetectedClock]] = []
    for bs in bundles:
        path = bs.bundle.clock_signal
        if path:
            resolved = _try_paths(path, tb_prefix)
            last_error: ClockDetectError | None = None
            clock = None
            for candidate in resolved:
                try:
                    clock = resolve_bundle_clock(waveform, candidate)
                    break
                except ClockDetectError as e:
                    last_error = e
            if clock is None:
                raise WellenIngestError(
                    f"bundle {bs.bundle.name!r}: {last_error}"
                ) from None
        else:
            # Legacy fallback: single global autodetect, cached on
            # first miss.
            if global_clock is None:
                global_clock = detect_global_clock(waveform)
            clock = global_clock
        out.append((bs, clock))
    return out


def _emit_events(
    waveform: pywellen.Waveform,
    bundle_clocks: list[tuple[_BundleSignals, DetectedClock]],
    channel_acc: dict | None = None,
) -> Iterator[HandshakeEvent]:
    """Walk each bundle's clock posedges independently; emit a
    HandshakeEvent on any channel where valid && ready holds.

    Per-bundle iteration lets multi-clock-domain fabrics work
    correctly — different bundles can use different clocks.
    Events from different bundles interleave in posedge order;
    the downstream reconstruct stage tolerates any interleaving.

    ``channel_acc`` (optional) is a mutable dict the sampler updates
    with per-(bundle, channel) cycle counters — ``active`` (valid
    asserted), ``stall`` (valid && !ready → backpressure), and
    ``handshakes`` (valid && ready). The CLI reads it after the
    pipeline drains to fill ChannelStats util%/bp%/txns/beats, which
    the aggregate (transaction-only) can't compute.
    """
    timescale = waveform.hierarchy.timescale()
    from rtl_buddy_axi_profiler.stages.ingest._clock_detect import (
        _tick_to_fs,
        build_time_index,
        preedge_index,
    )

    tick_fs = _tick_to_fs(timescale.factor, timescale.unit)
    # Global time table (index -> trace time) so each posedge can be
    # sampled at its pre-edge entry via value_at_idx. Empty (no time
    # table) -> _sample_bundle falls back to value_at_time(tick).
    times = build_time_index(waveform)

    # Build a sorted (tick, bundle_index) event list across all
    # bundle clocks so the output stream is monotonic in t_fs.
    events: list[tuple[int, int]] = []
    for idx, (_bs, clock) in enumerate(bundle_clocks):
        for tick in clock.posedge_times:
            events.append((tick, idx))
    events.sort(key=lambda x: x[0])

    for tick, idx in events:
        bs = bundle_clocks[idx][0]
        t_fs = tick * tick_fs
        # Sample the values the design's flops latch at this posedge: the
        # time-table entry just before the edge. value_at_time(tick)
        # returns the *post*-edge value, so a single-cycle handshake whose
        # READY deasserts as the transfer completes reads valid&&!ready
        # -> 0 txns / 100% backpressure (issue #56).
        sidx = preedge_index(times, tick) if times else None
        yield from _sample_bundle(bs, tick, sidx, t_fs, channel_acc)


def _bump(channel_acc: dict | None, bundle: str, ch: Channel, v: bool, r: bool) -> None:
    """Update the per-(bundle, channel) cycle counters for one posedge."""
    if channel_acc is None or not v:
        return
    acc = channel_acc.setdefault(bundle, {}).setdefault(
        ch.value, {"active": 0, "stall": 0, "handshakes": 0}
    )
    acc["active"] += 1
    if r:
        acc["handshakes"] += 1
    else:
        acc["stall"] += 1


def _sample_bundle(
    bs: _BundleSignals,
    tick: int,
    sidx: int | None,
    t_fs: int,
    channel_acc: dict | None = None,
) -> Iterator[HandshakeEvent]:
    """For one bundle at one clock posedge: tally each channel's
    valid/ready occupancy (for util%/bp%) and emit a HandshakeEvent on
    each channel where valid && ready hold simultaneously.

    Signals are read at ``sidx`` (the pre-edge time-table index) when
    available, else at ``tick`` via ``value_at_time`` — see
    ``_emit_events`` / issue #56."""
    name = bs.bundle.name

    arv, arr = _high(bs.arvalid, tick, sidx), _high(bs.arready, tick, sidx)
    _bump(channel_acc, name, Channel.AR, arv, arr)
    if arv and arr:
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=name,
            channel=Channel.AR,
            txn_id=_int_at(bs.arid, tick, sidx),
            addr=_int_at(bs.araddr, tick, sidx),
            len_beats=_int_at(bs.arlen, tick, sidx),
            size_log2=_int_at(bs.arsize, tick, sidx),
        )

    awv, awr = _high(bs.awvalid, tick, sidx), _high(bs.awready, tick, sidx)
    _bump(channel_acc, name, Channel.AW, awv, awr)
    if awv and awr:
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=name,
            channel=Channel.AW,
            txn_id=_int_at(bs.awid, tick, sidx),
            addr=_int_at(bs.awaddr, tick, sidx),
            len_beats=_int_at(bs.awlen, tick, sidx),
            size_log2=_int_at(bs.awsize, tick, sidx),
        )

    rv, rr = _high(bs.rvalid, tick, sidx), _high(bs.rready, tick, sidx)
    _bump(channel_acc, name, Channel.R, rv, rr)
    if rv and rr:
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=name,
            channel=Channel.R,
            txn_id=_int_at(bs.rid, tick, sidx),
            resp=_int_at(bs.rresp, tick, sidx),
            last=bool(_int_at(bs.rlast, tick, sidx)),
        )

    wv, wr = _high(bs.wvalid, tick, sidx), _high(bs.wready, tick, sidx)
    _bump(channel_acc, name, Channel.W, wv, wr)
    if wv and wr:
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=name,
            channel=Channel.W,
            last=bool(_int_at(bs.wlast, tick, sidx)),
        )

    bv, br = _high(bs.bvalid, tick, sidx), _high(bs.bready, tick, sidx)
    _bump(channel_acc, name, Channel.B, bv, br)
    if bv and br:
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=name,
            channel=Channel.B,
            txn_id=_int_at(bs.bid, tick, sidx),
            resp=_int_at(bs.bresp, tick, sidx),
        )


def _high(signal: pywellen.Signal, tick: int, sidx: int | None = None) -> bool:
    """True iff the signal's value is 1 at the pre-edge index ``sidx``
    (preferred) or, as a fallback, at trace time ``tick``."""
    val = signal.value_at_idx(sidx) if sidx is not None else signal.value_at_time(tick)
    return _to_int(val) == 1


def _int_at(signal: pywellen.Signal | None, tick: int, sidx: int | None = None) -> int:
    if signal is None:
        return 0
    val = signal.value_at_idx(sidx) if sidx is not None else signal.value_at_time(tick)
    return _to_int(val)


def _to_int(value) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    # Multi-bit signals come back as binary strings.
    if isinstance(value, str):
        try:
            return int(value, 2)
        except ValueError:
            return 0
    return 0


def _resolve_bundles(
    waveform: pywellen.Waveform,
    bundles_by_name: dict[str, Bundle],
    *,
    tb_prefix: str = "",
) -> list[_BundleSignals]:
    """Look up every required signal handle once; bail on missing
    signals so the user can fix the manifest before the long sim run."""
    out: list[_BundleSignals] = []
    for bundle in bundles_by_name.values():
        try:
            out.append(_resolve_bundle(waveform, bundle, tb_prefix=tb_prefix))
        except WellenIngestError as e:
            raise WellenIngestError(f"bundle {bundle.name!r}: {e}") from None
    return out


def _try_paths(path: str, tb_prefix: str) -> list[str]:
    """Candidate signal paths to try, in priority order.

    Direct path first (most precise); tb_prefix-prepended fallback
    second. An empty tb_prefix collapses to just the direct path.
    """
    if not tb_prefix:
        return [path]
    return [path, f"{tb_prefix.rstrip('.')}.{path}"]


def _resolve_bundle(
    waveform: pywellen.Waveform, bundle: Bundle, *, tb_prefix: str = ""
) -> _BundleSignals:
    def _lookup(path: str) -> pywellen.Signal | None:
        for candidate in _try_paths(path, tb_prefix):
            try:
                return waveform.get_signal_from_path(candidate)
            except RuntimeError:
                # pywellen raises RuntimeError("No var at path ...") on a
                # genuine miss. Catch only that: anything else (e.g. the
                # AttributeError from an incompatible pywellen rewriting
                # the Waveform API, #52) must propagate loudly instead of
                # masquerading as "signal not found in trace".
                continue
        return None

    def required(role: str) -> pywellen.Signal:
        path = bundle.signals.get(role)
        if not path:
            raise WellenIngestError(f"missing required signal {role!r}")
        sig = _lookup(path)
        if sig is None:
            raise WellenIngestError(
                f"signal {path!r} (role={role!r}) not found in trace; "
                f"tried {_try_paths(path, tb_prefix)}"
            )
        return sig

    def optional(role: str) -> pywellen.Signal | None:
        path = bundle.signals.get(role)
        if not path:
            return None
        return _lookup(path)

    return _BundleSignals(
        bundle=bundle,
        arvalid=required("arvalid"),
        arready=required("arready"),
        araddr=optional("araddr"),
        arid=optional("arid"),
        arlen=optional("arlen"),
        arsize=optional("arsize"),
        awvalid=required("awvalid"),
        awready=required("awready"),
        awaddr=optional("awaddr"),
        awid=optional("awid"),
        awlen=optional("awlen"),
        awsize=optional("awsize"),
        rvalid=required("rvalid"),
        rready=required("rready"),
        rid=optional("rid"),
        rresp=optional("rresp"),
        rlast=optional("rlast"),
        wvalid=required("wvalid"),
        wready=required("wready"),
        wlast=optional("wlast"),
        bvalid=required("bvalid"),
        bready=required("bready"),
        bid=optional("bid"),
        bresp=optional("bresp"),
    )


def _flat_bundles(bundles: tuple[Bundle, ...]) -> dict[str, Bundle]:
    out: dict[str, Bundle] = {}
    for b in bundles:
        out[b.name] = b
        for child in b.children:
            out[child.name] = child
    return out


class WellenIngest:
    """Ingest Protocol implementation (entry-point registration target)."""

    name = "wellen"

    def __init__(self, *, tb_prefix: str = "") -> None:
        self._detected_clock: DetectedClock | None = None
        self._bundle_clocks: list[tuple[str, DetectedClock]] = []
        self.tb_prefix = tb_prefix
        # Per-(bundle_name, channel) cycle counters, filled lazily as the
        # event generator is consumed: {bundle: {ch: {active, stall,
        # handshakes}}}. Read by the CLI after the pipeline drains to
        # populate ChannelStats util%/bp%/txns/beats.
        self.channel_cycle_stats: dict = {}

    @property
    def detected_clock(self) -> DetectedClock | None:
        """The single representative clock used by the CLI to plumb
        duration_cycles / clock_period_ns into the aggregate stage.

        With per-bundle clocks the CLI picks the *fastest* clock so
        ``duration_cycles`` is the worst-case cycle count. Improved
        per-bundle metadata propagation is a follow-up if/when the
        aggregate stage grows multi-clock awareness.
        """
        return self._detected_clock

    @property
    def bundle_clocks(self) -> list[tuple[str, DetectedClock]]:
        """``[(bundle_name, DetectedClock), ...]`` — each bundle's
        resolved clock after the first :meth:`run`."""
        return self._bundle_clocks

    def run(self, source: Path, manifest: Manifest) -> Iterator[HandshakeEvent]:
        try:
            waveform = pywellen.Waveform(str(source))
        except Exception as e:
            raise WellenIngestError(f"could not open trace {source}: {e}") from None
        bundles = _resolve_bundles(
            waveform, _flat_bundles(manifest.bundles), tb_prefix=self.tb_prefix
        )
        bundle_clocks = _resolve_bundle_clocks(
            waveform, bundles, tb_prefix=self.tb_prefix
        )
        self._bundle_clocks = [(bs.bundle.name, clock) for bs, clock in bundle_clocks]
        if bundle_clocks:
            # Pick the clock with the most posedges as the representative —
            # i.e. the fastest clock observed across all bundles. The CLI
            # uses this for duration_cycles when threading to aggregate.
            self._detected_clock = max(
                (clock for _, clock in bundle_clocks),
                key=lambda c: len(c.posedge_times),
            )
        else:
            self._detected_clock = None
        self.channel_cycle_stats = {}
        return _emit_events(waveform, bundle_clocks, self.channel_cycle_stats)
