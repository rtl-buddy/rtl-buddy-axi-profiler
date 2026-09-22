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

pywellen API: this module drives the >=0.25 random-access surface —
``wf.timescale`` / ``wf.all_vars()`` / ``wf[path]`` / ``Var.signal`` /
``sig[:]``. See ``_pywellen_compat`` for the guard and the history.
"""

from __future__ import annotations

from dataclasses import dataclass

import pywellen

from rtl_buddy_axi_profiler.stages.ingest._pywellen_compat import lookup_signal


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
    timescale = waveform.timescale
    if timescale is None:
        raise ClockDetectError("trace has no timescale; cannot derive a clock period.")
    tick_fs = _tick_to_fs(timescale.factor, timescale.unit)

    best: tuple[int, str, tuple[int, ...]] | None = None
    for var in waveform.all_vars():
        # >=0.25: bitwidth / full_name / signal are zero-arg properties
        # (they took the hierarchy as an argument before). bitwidth is
        # None for reals and strings, which `!= 1` filters out too.
        if var.bitwidth != 1:
            continue
        name = var.full_name
        sig = var.signal
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
    timescale = waveform.timescale
    if timescale is None:
        raise ClockDetectError("trace has no timescale; cannot derive a clock period.")
    tick_fs = _tick_to_fs(timescale.factor, timescale.unit)

    sig = lookup_signal(waveform, clock_signal_path)
    if sig is None:
        raise ClockDetectError(
            f"clock signal {clock_signal_path!r} not found in trace; "
            f"check the bundle's clock_signal against the trace's hierarchy."
        )

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
    # ``sig[:]`` is the >=0.25 change vector: a time-ordered list of
    # ``(int time, value)``. It replaces ``Signal.all_changes()``.
    for t, value in signal[:]:
        # Wellen yields int values for fully-2-state samples; an x/z bit
        # comes back as a bit-string. A clock candidate that is ever x/z
        # isn't 0 or 1 at that sample, so int() would raise — map it to a
        # sentinel that can't form a posedge instead.
        if isinstance(value, int):
            v: int = value
        else:
            try:
                v = int(str(value), 2)
            except ValueError:
                v = -1
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
