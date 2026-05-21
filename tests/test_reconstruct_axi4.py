"""Tests for the AXI4 reconstruct stage.

Synthetic HandshakeEvent streams; no real waveforms here. Each test
constructs an explicit sequence of handshakes and asserts the
Transaction output is correct in shape, content, and ordering.
"""

from __future__ import annotations

from rtl_buddy_axi_profiler.stages.reconstruct.axi4 import (
    AXI4Reconstruct,
    reconstruct,
)
from rtl_buddy_axi_profiler.types import Channel, HandshakeEvent


def _ar(
    *,
    bundle: str = "b",
    t: int,
    txn_id: int,
    addr: int = 0x100,
    len_beats: int = 0,
    size_log2: int = 3,
) -> HandshakeEvent:
    return HandshakeEvent(
        t_fs=t,
        bundle_name=bundle,
        channel=Channel.AR,
        txn_id=txn_id,
        addr=addr,
        len_beats=len_beats,
        size_log2=size_log2,
    )


def _r(
    *,
    bundle: str = "b",
    t: int,
    txn_id: int,
    last: bool = True,
    resp: int = 0,
) -> HandshakeEvent:
    return HandshakeEvent(
        t_fs=t,
        bundle_name=bundle,
        channel=Channel.R,
        txn_id=txn_id,
        last=last,
        resp=resp,
    )


def _aw(
    *, bundle: str = "b", t: int, txn_id: int, addr: int = 0x200, len_beats: int = 0
) -> HandshakeEvent:
    return HandshakeEvent(
        t_fs=t,
        bundle_name=bundle,
        channel=Channel.AW,
        txn_id=txn_id,
        addr=addr,
        len_beats=len_beats,
    )


def _w(*, bundle: str = "b", t: int, last: bool = True) -> HandshakeEvent:
    return HandshakeEvent(t_fs=t, bundle_name=bundle, channel=Channel.W, last=last)


def _b(*, bundle: str = "b", t: int, txn_id: int, resp: int = 0) -> HandshakeEvent:
    return HandshakeEvent(
        t_fs=t,
        bundle_name=bundle,
        channel=Channel.B,
        txn_id=txn_id,
        resp=resp,
    )


def test_single_read_burst_of_one_beat() -> None:
    events = [_ar(t=10, txn_id=1), _r(t=30, txn_id=1, last=True)]
    txns = list(reconstruct(iter(events)))
    assert len(txns) == 1
    txn = txns[0]
    assert txn.is_read is True
    assert txn.txn_id == 1
    assert txn.addr == 0x100
    assert txn.t_start_fs == 10
    assert txn.t_first_data_fs == 30
    assert txn.t_end_fs == 30


def test_read_multi_beat_completes_on_rlast() -> None:
    events = [
        _ar(t=10, txn_id=2, len_beats=3),
        _r(t=20, txn_id=2, last=False),
        _r(t=25, txn_id=2, last=False),
        _r(t=30, txn_id=2, last=False),
        _r(t=35, txn_id=2, last=True),
    ]
    txns = list(reconstruct(iter(events)))
    assert len(txns) == 1
    assert txns[0].t_first_data_fs == 20
    assert txns[0].t_end_fs == 35


def test_single_write_burst() -> None:
    events = [
        _aw(t=5, txn_id=1, len_beats=1),
        _w(t=8, last=False),
        _w(t=12, last=True),
        _b(t=20, txn_id=1),
    ]
    txns = list(reconstruct(iter(events)))
    assert len(txns) == 1
    txn = txns[0]
    assert txn.is_read is False
    assert txn.t_start_fs == 5
    assert txn.t_end_fs == 20


def test_out_of_order_reads() -> None:
    """Reads can return in a different order than they were requested."""
    events = [
        _ar(t=10, txn_id=1),
        _ar(t=12, txn_id=2),
        _r(t=30, txn_id=2, last=True),  # txn 2 returns first
        _r(t=40, txn_id=1, last=True),
    ]
    txns = list(reconstruct(iter(events)))
    assert len(txns) == 2
    # Order of yield matches order of completion.
    assert txns[0].txn_id == 2
    assert txns[0].t_end_fs == 30
    assert txns[1].txn_id == 1
    assert txns[1].t_end_fs == 40


def test_read_with_slverr() -> None:
    events = [
        _ar(t=10, txn_id=3),
        _r(t=20, txn_id=3, last=True, resp=2),  # SLVERR
    ]
    txns = list(reconstruct(iter(events)))
    assert txns[0].resp == 2


def test_write_resp_carries_through_to_transaction() -> None:
    events = [
        _aw(t=5, txn_id=1),
        _w(t=10, last=True),
        _b(t=20, txn_id=1, resp=3),  # DECERR
    ]
    txns = list(reconstruct(iter(events)))
    assert txns[0].resp == 3


def test_w_without_aw_is_dropped() -> None:
    """Spurious W with empty AW FIFO does not crash and yields nothing."""
    events = [_w(t=10, last=True)]
    txns = list(reconstruct(iter(events)))
    assert txns == []


def test_r_without_ar_is_dropped() -> None:
    """Spurious R with no matching AR yields nothing, no crash."""
    events = [_r(t=10, txn_id=99, last=True)]
    txns = list(reconstruct(iter(events)))
    assert txns == []


def test_two_concurrent_writes_with_distinct_ids() -> None:
    """W beats FIFO-pair to AW order; B returns may be out of order."""
    events = [
        _aw(t=5, txn_id=1, len_beats=1),
        _aw(t=6, txn_id=2, len_beats=0),
        _w(t=8, last=False),  # txn 1 beat 0
        _w(t=12, last=True),  # txn 1 beat 1 → completes W for txn 1
        _w(t=14, last=True),  # txn 2 beat 0 → completes W for txn 2
        _b(t=20, txn_id=2),  # txn 2 closes first
        _b(t=25, txn_id=1),  # txn 1 closes second
    ]
    txns = list(reconstruct(iter(events)))
    assert len(txns) == 2
    assert txns[0].txn_id == 2
    assert txns[0].t_end_fs == 20
    assert txns[1].txn_id == 1
    assert txns[1].t_end_fs == 25


def test_two_bundles_dont_interfere() -> None:
    """Per-bundle state is isolated."""
    events = [
        _ar(bundle="A", t=10, txn_id=1),
        _ar(bundle="B", t=12, txn_id=1),
        _r(bundle="B", t=20, txn_id=1, last=True),
        _r(bundle="A", t=30, txn_id=1, last=True),
    ]
    txns = list(reconstruct(iter(events)))
    assert len(txns) == 2
    assert txns[0].bundle_name == "B"
    assert txns[1].bundle_name == "A"


def test_axi4_reconstruct_wrapper_is_iterable() -> None:
    """The Protocol wrapper yields the same Transactions."""
    events = [_ar(t=10, txn_id=1), _r(t=20, txn_id=1, last=True)]
    txns = list(AXI4Reconstruct().run(iter(events)))
    assert len(txns) == 1
    assert txns[0].t_end_fs == 20


def test_pending_at_end_of_stream_is_silently_dropped() -> None:
    """Unfinished transactions are not emitted (no RLAST/B handshake)."""
    events = [_ar(t=10, txn_id=1)]  # no closing R
    txns = list(reconstruct(iter(events)))
    assert txns == []
