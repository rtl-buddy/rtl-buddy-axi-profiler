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


def ingest(source: Path, manifest: Manifest) -> Iterator[HandshakeEvent]:
    """Yield HandshakeEvent objects from a waveform file.

    See :class:`WellenIngest` for the entry-point class wrapper.
    """
    try:
        waveform = pywellen.Waveform(str(source))
    except Exception as e:
        raise WellenIngestError(f"could not open trace {source}: {e}") from None

    bundles = _resolve_bundles(waveform, _flat_bundles(manifest.bundles))
    bundle_clocks = _resolve_bundle_clocks(waveform, bundles)
    yield from _emit_events(waveform, bundle_clocks)


def _resolve_bundle_clocks(
    waveform: pywellen.Waveform, bundles: list[_BundleSignals]
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
            try:
                clock = resolve_bundle_clock(waveform, path)
            except ClockDetectError as e:
                raise WellenIngestError(f"bundle {bs.bundle.name!r}: {e}") from None
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
) -> Iterator[HandshakeEvent]:
    """Walk each bundle's clock posedges independently; emit a
    HandshakeEvent on any channel where valid && ready holds.

    Per-bundle iteration lets multi-clock-domain fabrics work
    correctly — different bundles can use different clocks.
    Events from different bundles interleave in posedge order;
    the downstream reconstruct stage tolerates any interleaving.
    """
    timescale = waveform.hierarchy.timescale()
    from rtl_buddy_axi_profiler.stages.ingest._clock_detect import _tick_to_fs

    tick_fs = _tick_to_fs(timescale.factor, timescale.unit)

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
        yield from _sample_bundle(bs, tick, t_fs)


def _sample_bundle(
    bs: _BundleSignals, tick: int, t_fs: int
) -> Iterator[HandshakeEvent]:
    """For one bundle at one clock posedge, emit a HandshakeEvent on
    each channel where valid && ready hold simultaneously."""
    if _high(bs.arvalid, tick) and _high(bs.arready, tick):
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=bs.bundle.name,
            channel=Channel.AR,
            txn_id=_int_at(bs.arid, tick),
            addr=_int_at(bs.araddr, tick),
            len_beats=_int_at(bs.arlen, tick),
            size_log2=_int_at(bs.arsize, tick),
        )
    if _high(bs.awvalid, tick) and _high(bs.awready, tick):
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=bs.bundle.name,
            channel=Channel.AW,
            txn_id=_int_at(bs.awid, tick),
            addr=_int_at(bs.awaddr, tick),
            len_beats=_int_at(bs.awlen, tick),
            size_log2=_int_at(bs.awsize, tick),
        )
    if _high(bs.rvalid, tick) and _high(bs.rready, tick):
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=bs.bundle.name,
            channel=Channel.R,
            txn_id=_int_at(bs.rid, tick),
            resp=_int_at(bs.rresp, tick),
            last=bool(_int_at(bs.rlast, tick)),
        )
    if _high(bs.wvalid, tick) and _high(bs.wready, tick):
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=bs.bundle.name,
            channel=Channel.W,
            last=bool(_int_at(bs.wlast, tick)),
        )
    if _high(bs.bvalid, tick) and _high(bs.bready, tick):
        yield HandshakeEvent(
            t_fs=t_fs,
            bundle_name=bs.bundle.name,
            channel=Channel.B,
            txn_id=_int_at(bs.bid, tick),
            resp=_int_at(bs.bresp, tick),
        )


def _high(signal: pywellen.Signal, tick: int) -> bool:
    """True iff the signal's value at ``tick`` is 1."""
    val = signal.value_at_time(tick)
    return _to_int(val) == 1


def _int_at(signal: pywellen.Signal | None, tick: int) -> int:
    if signal is None:
        return 0
    val = signal.value_at_time(tick)
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
    waveform: pywellen.Waveform, bundles_by_name: dict[str, Bundle]
) -> list[_BundleSignals]:
    """Look up every required signal handle once; bail on missing
    signals so the user can fix the manifest before the long sim run."""
    out: list[_BundleSignals] = []
    for bundle in bundles_by_name.values():
        try:
            out.append(_resolve_bundle(waveform, bundle))
        except WellenIngestError as e:
            raise WellenIngestError(f"bundle {bundle.name!r}: {e}") from None
    return out


def _resolve_bundle(waveform: pywellen.Waveform, bundle: Bundle) -> _BundleSignals:
    def required(role: str) -> pywellen.Signal:
        path = bundle.signals.get(role)
        if not path:
            raise WellenIngestError(f"missing required signal {role!r}")
        try:
            return waveform.get_signal_from_path(path)
        except Exception:
            raise WellenIngestError(
                f"signal {path!r} (role={role!r}) not found in trace"
            ) from None

    def optional(role: str) -> pywellen.Signal | None:
        path = bundle.signals.get(role)
        if not path:
            return None
        try:
            return waveform.get_signal_from_path(path)
        except Exception:
            return None

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

    def __init__(self) -> None:
        self._detected_clock: DetectedClock | None = None
        self._bundle_clocks: list[tuple[str, DetectedClock]] = []

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
        bundles = _resolve_bundles(waveform, _flat_bundles(manifest.bundles))
        bundle_clocks = _resolve_bundle_clocks(waveform, bundles)
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
        return _emit_events(waveform, bundle_clocks)
