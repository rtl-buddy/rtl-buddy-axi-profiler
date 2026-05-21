"""Five-stage pipeline Protocol contracts.

Each Protocol describes the surface a stage must implement to be
swappable. Variants register via the `rtl_buddy_axi_profiler.stages`
entry-point group; see `pyproject.toml`.

No stage implementations live here — the bootstrap ships the
contracts only. The pipeline stages land in issues #2, #3, #4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

from rtl_buddy_axi_profiler.types import (
    AggregateStats,
    HandshakeEvent,
    Manifest,
    Transaction,
)


@runtime_checkable
class Discover(Protocol):
    """Stage 1: walk RTL sources and emit a Manifest of bundles."""

    name: str

    def run(self, filelist: Path, top: str) -> Manifest: ...


@runtime_checkable
class Ingest(Protocol):
    """Stage 2: yield per-channel handshake events from a trace.

    Implementations are expected to stream — do not materialize the
    full event list. Long sims will overrun memory otherwise.
    """

    name: str

    def run(self, source: Path, manifest: Manifest) -> Iterator[HandshakeEvent]: ...


@runtime_checkable
class Reconstruct(Protocol):
    """Stage 3: turn an event stream into a transaction stream.

    Stateful per (bundle, channel-pair). Out-of-order R returns are
    handled with ID-keyed pending tables.
    """

    name: str

    def run(self, events: Iterator[HandshakeEvent]) -> Iterator[Transaction]: ...


@runtime_checkable
class Aggregate(Protocol):
    """Stage 4: accumulate per-bundle stats and interconnect roll-ups."""

    name: str

    def run(
        self, txns: Iterator[Transaction], manifest: Manifest
    ) -> AggregateStats: ...


@runtime_checkable
class Emit(Protocol):
    """Stage 5: serialize the v1 axi-perf.json to disk.

    Implementations validate against `schema/axi_perf_v1.json` before
    writing — a malformed roll-up is caught here, not in the consumer.
    """

    name: str

    def run(self, stats: AggregateStats, manifest: Manifest, out: Path) -> None: ...
