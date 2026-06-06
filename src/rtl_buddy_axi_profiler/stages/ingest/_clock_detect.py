"""Clock detection / resolution from a waveform.

Two paths:

1. **Per-bundle (preferred)** — :func:`resolve_bundle_clock` reads
   the manifest's ``Bundle.clock_signal`` and walks that exact
   signal's posedges. Each AXI interface carries its own clock pin
   so multi-clock fabrics work correctly.

2. **Global fallback** — :func:`detect_global_clock` finds the
   highest-frequency 1-bit toggling signal in the trace. Kept for
   producers that ship manifests without ``clock_signal`` set
   (legacy v1.0 manifests, hand-written stubs).

The clock's period (in fs) becomes the cycle-count basis for all
downstream latency / throughput math.
"""

from __future__ import annotations

from dataclasses import dataclass

import pywellen


@dataclass(frozen=True)
class DetectedClock:
    """Result of clock autodetection."""

    full_name: str
    period_fs: int
    posedge_times: tuple[int, ...]
    """Absolute trace times (in the trace's time-unit ticks) at which
    the clock signal transitioned 0 → 1. The ingest stage iterates
    this to sample handshake states."""


class ClockDetectError(ValueError):
    """Raised when no plausible clock signal was found in the trace."""


def detect_global_clock(waveform: pywellen.Waveform) -> DetectedClock:
    """Find the highest-frequency 1-bit toggling signal — that's the
    global AXI clock for the bundle pool.

    v1 assumes a single global clock for the fabric; mixed-domain
    designs are tracked as a follow-up. The fallback when the trace
    has only a single bit-toggling signal still returns it (so a
    minimal single-clock fixture works).
    """
    timescale = waveform.hierarchy.timescale()
    if timescale is None:
        raise ClockDetectError("trace has no timescale; cannot derive a clock period.")
    tick_fs = _tick_to_fs(timescale.factor, timescale.unit)

    best: tuple[int, str, tuple[int, ...]] | None = None
    for var in waveform.hierarchy.all_vars():
        if var.bitwidth() != 1:
            continue
        name = var.full_name(waveform.hierarchy)
        sig = waveform.get_signal(var)
        posedges = _posedge_times(sig)
        if len(posedges) < 2:
            continue
        # Score: more posedges = more likely a clock. Ties broken by
        # name ordering (deterministic) and shorter periods (faster
        # clocks dominate large designs).
        score = len(posedges)
        if best is None or score > best[0]:
            best = (score, name, posedges)

    if best is None:
        raise ClockDetectError(
            "no toggling 1-bit signal found in the trace; can't infer a clock."
        )

    _, name, posedges = best
    period_ticks = posedges[1] - posedges[0]
    return DetectedClock(
        full_name=name,
        period_fs=period_ticks * tick_fs,
        posedge_times=posedges,
    )


def resolve_bundle_clock(
    waveform: pywellen.Waveform, clock_signal_path: str
) -> DetectedClock:
    """Look up a specific clock signal by its trace path.

    Used when ``Bundle.clock_signal`` is set (the manifest names the
    bundle's clock explicitly). Raises :class:`ClockDetectError`
    if the signal isn't in the trace, isn't 1-bit, or has fewer
    than two posedges.
    """
    timescale = waveform.hierarchy.timescale()
    if timescale is None:
        raise ClockDetectError("trace has no timescale; cannot derive a clock period.")
    tick_fs = _tick_to_fs(timescale.factor, timescale.unit)

    try:
        sig = waveform.get_signal_from_path(clock_signal_path)
    except RuntimeError:
        # pywellen's genuine-miss exception; an incompatible pywellen
        # (e.g. the 0.25 API rewrite, #52) raises AttributeError instead
        # and must propagate rather than read as a bad clock_signal.
        raise ClockDetectError(
            f"clock signal {clock_signal_path!r} not found in trace; "
            f"check the bundle's clock_signal against the trace's hierarchy."
        ) from None

    posedges = _posedge_times(sig)
    if len(posedges) < 2:
        raise ClockDetectError(
            f"clock signal {clock_signal_path!r} has fewer than two posedges; "
            f"cannot derive a period."
        )
    period_ticks = posedges[1] - posedges[0]
    return DetectedClock(
        full_name=clock_signal_path,
        period_fs=period_ticks * tick_fs,
        posedge_times=posedges,
    )


def _posedge_times(signal: pywellen.Signal) -> tuple[int, ...]:
    """Return (time, ...) for every 0 → 1 transition on a 1-bit signal."""
    edges: list[int] = []
    prev: int | None = None
    for t, value in signal.all_changes():
        # Wellen yields int values for 1-bit signals; multibit returns
        # the bit-string. We only call this on 1-bit signals.
        v = int(value) if not isinstance(value, int) else value
        if prev == 0 and v == 1:
            edges.append(t)
        prev = v
    return tuple(edges)


def _tick_to_fs(factor: int, unit: str) -> int:
    """Convert (factor, unit) from the trace's timescale into fs/tick."""
    multipliers = {
        "fs": 1,
        "ps": 1_000,
        "ns": 1_000_000,
        "us": 1_000_000_000,
        "ms": 1_000_000_000_000,
        "s": 1_000_000_000_000_000,
    }
    unit_lc = str(unit).lower()
    if unit_lc not in multipliers:
        raise ClockDetectError(f"unknown timescale unit {unit!r}")
    return factor * multipliers[unit_lc]
