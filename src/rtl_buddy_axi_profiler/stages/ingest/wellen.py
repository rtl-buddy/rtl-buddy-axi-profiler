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
    DetectedClock,
    detect_global_clock,
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

    clock = detect_global_clock(waveform)
    bundles = _resolve_bundles(waveform, _flat_bundles(manifest.bundles))
    yield from _emit_events(waveform, clock, bundles)


def _emit_events(
    waveform: pywellen.Waveform,
    clock: DetectedClock,
    bundles: list[_BundleSignals],
) -> Iterator[HandshakeEvent]:
    """Walk clock posedges and emit one HandshakeEvent per active
    (channel, bundle) at each posedge."""
    timescale = waveform.hierarchy.timescale()
    from rtl_buddy_axi_profiler.stages.ingest._clock_detect import _tick_to_fs

    tick_fs = _tick_to_fs(timescale.factor, timescale.unit)

    for tick in clock.posedge_times:
        t_fs = tick * tick_fs
        for bs in bundles:
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

    @property
    def detected_clock(self) -> DetectedClock | None:
        """Set after the first :meth:`run` so the CLI can read clock
        metadata for downstream stages."""
        return self._detected_clock

    def run(self, source: Path, manifest: Manifest) -> Iterator[HandshakeEvent]:
        try:
            waveform = pywellen.Waveform(str(source))
        except Exception as e:
            raise WellenIngestError(f"could not open trace {source}: {e}") from None
        self._detected_clock = detect_global_clock(waveform)
        bundles = _resolve_bundles(waveform, _flat_bundles(manifest.bundles))
        return _emit_events(waveform, self._detected_clock, bundles)
