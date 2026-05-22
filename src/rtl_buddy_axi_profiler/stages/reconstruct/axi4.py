"""AXI4 transaction reconstruct stage.

Walks a stream of :class:`HandshakeEvent` and emits one
:class:`Transaction` per completed read / write. Reads complete on
RLAST with the matching ID; writes complete on B with the matching
ID. Out-of-order R / B returns are handled via ID-keyed pending
tables — no FIFO assumption.

Per the AXI4 spec, the W channel has no WID; data beats are issued
in the same order as the AW requests on a given master interface.
This stage uses FIFO ordering for W↔AW pairing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterator

from rtl_buddy_axi_profiler.types import Channel, HandshakeEvent, Transaction


@dataclass
class _PendingRead:
    """Per-(bundle, id) read state."""

    txn_id: int
    addr: int
    len_beats: int
    size_log2: int
    t_ar_fs: int
    t_first_data_fs: int = 0
    beats_seen: int = 0
    resp: int = 0


@dataclass
class _PendingWrite:
    """Per-(bundle, id) write state."""

    txn_id: int
    addr: int
    len_beats: int
    size_log2: int
    t_aw_fs: int
    beats_seen: int = 0


@dataclass
class _BundleState:
    """Per-bundle reconstruct state."""

    # ID-keyed pending tables for AR/R and AW/W/B.
    pending_reads: dict[int, _PendingRead] = field(default_factory=dict)
    pending_writes: dict[int, _PendingWrite] = field(default_factory=dict)
    # AW queue, FIFO matched to W beats (W has no ID in AXI4).
    aw_fifo: deque[int] = field(default_factory=deque)


def reconstruct(events: Iterator[HandshakeEvent]) -> Iterator[Transaction]:
    """Yield completed Transactions in the order they close.

    Pending state at end-of-stream is silently dropped — a transaction
    that never sees its closing handshake won't appear in the output.
    The downstream aggregate stage is responsible for noting any
    abandoned txns; this stage does not.
    """
    state: dict[str, _BundleState] = {}
    for event in events:
        bundle = state.setdefault(event.bundle_name, _BundleState())
        yield from _handle_event(bundle, event)


def _handle_event(bundle: _BundleState, event: HandshakeEvent) -> Iterator[Transaction]:
    if event.channel == Channel.AR:
        bundle.pending_reads[event.txn_id] = _PendingRead(
            txn_id=event.txn_id,
            addr=event.addr,
            len_beats=event.len_beats,
            size_log2=event.size_log2,
            t_ar_fs=event.t_fs,
        )
        return

    if event.channel == Channel.R:
        read_pending = bundle.pending_reads.get(event.txn_id)
        if read_pending is None:
            # Spurious R — no matching AR. Drop silently.
            return
        if read_pending.beats_seen == 0:
            read_pending.t_first_data_fs = event.t_fs
        read_pending.beats_seen += 1
        if event.resp:
            read_pending.resp = max(read_pending.resp, event.resp)
        if event.last:
            del bundle.pending_reads[event.txn_id]
            yield _read_to_transaction(event.bundle_name, read_pending, event.t_fs)
        return

    if event.channel == Channel.AW:
        bundle.pending_writes[event.txn_id] = _PendingWrite(
            txn_id=event.txn_id,
            addr=event.addr,
            len_beats=event.len_beats,
            size_log2=event.size_log2,
            t_aw_fs=event.t_fs,
        )
        bundle.aw_fifo.append(event.txn_id)
        return

    if event.channel == Channel.W:
        # W has no ID in AXI4 — pair with the head of the AW FIFO.
        if not bundle.aw_fifo:
            return
        head_id = bundle.aw_fifo[0]
        write_pending = bundle.pending_writes.get(head_id)
        if write_pending is None:
            bundle.aw_fifo.popleft()
            return
        write_pending.beats_seen += 1
        if event.last:
            bundle.aw_fifo.popleft()
        return

    if event.channel == Channel.B:
        b_pending = bundle.pending_writes.pop(event.txn_id, None)
        if b_pending is None:
            return
        yield _write_to_transaction(event.bundle_name, b_pending, event)
        return


def _read_to_transaction(
    bundle_name: str, p: _PendingRead, t_end_fs: int
) -> Transaction:
    return Transaction(
        bundle_name=bundle_name,
        is_read=True,
        txn_id=p.txn_id,
        addr=p.addr,
        # AxLEN encodes "burst length minus 1"; Transaction.len_beats
        # is documented as actual beat count, so convert here. The
        # in-flight _PendingRead keeps the raw AxLEN value the AR
        # event delivered.
        len_beats=p.len_beats + 1,
        size_log2=p.size_log2,
        t_start_fs=p.t_ar_fs,
        t_first_data_fs=p.t_first_data_fs,
        t_end_fs=t_end_fs,
        resp=p.resp,
    )


def _write_to_transaction(
    bundle_name: str, p: _PendingWrite, b_event: HandshakeEvent
) -> Transaction:
    return Transaction(
        bundle_name=bundle_name,
        is_read=False,
        txn_id=p.txn_id,
        addr=p.addr,
        # See _read_to_transaction: raw AxLEN → actual beats.
        len_beats=p.len_beats + 1,
        size_log2=p.size_log2,
        t_start_fs=p.t_aw_fs,
        t_first_data_fs=p.t_aw_fs,
        t_end_fs=b_event.t_fs,
        resp=b_event.resp,
    )


class AXI4Reconstruct:
    """:class:`Reconstruct` Protocol implementation."""

    name = "axi4"

    def run(self, events: Iterator[HandshakeEvent]) -> Iterator[Transaction]:
        return reconstruct(events)
