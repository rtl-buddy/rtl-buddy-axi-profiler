"""Pipeline data types shared across stages.

These dataclasses are the in-memory currency between the five stages.
They are intentionally light — the wire contracts that cross repo
boundaries live in `schema/` as JSON Schemas. The mapping between the
two is straightforward and lives in `stages/emit/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Channel(str, Enum):
    AR = "ar"
    AW = "aw"
    R = "r"
    W = "w"
    B = "b"


class Protocol(str, Enum):
    AXI4 = "AXI4"
    AXI_LITE = "AXI-Lite"
    AXI5 = "AXI5"


class DefaultView(str, Enum):
    PARENT = "parent"
    CHILDREN = "children"
    BOTH = "both"


class BundleSource(str, Enum):
    VERIBLE_INTERFACE = "verible-interface"
    VERIBLE_REGEX = "verible-regex"
    USER = "user"


@dataclass(frozen=True)
class Bundle:
    """One AXI bundle: a master → slave connection bearing five channels."""

    name: str
    master_path: str
    slave_path: str
    protocol: Protocol = Protocol.AXI4
    data_width: int = 64
    id_width: int = 4
    source: BundleSource = BundleSource.USER
    default_view: DefaultView = DefaultView.PARENT
    signals: dict[str, str] = field(default_factory=dict)
    children: tuple["Bundle", ...] = ()


@dataclass(frozen=True)
class Manifest:
    """In-memory shape of axi-bundles.yaml."""

    schema_version: str
    design_top: str
    bundles: tuple[Bundle, ...]
    generated_by: str = ""
    generated_at: str = ""


@dataclass(frozen=True)
class HandshakeEvent:
    """A single (valid && ready) crossing on a channel.

    Time is expressed in femtoseconds for parity with FST/VCD readers.
    """

    t_fs: int
    bundle_name: str
    channel: Channel
    txn_id: int = 0
    addr: int = 0
    resp: int = 0
    last: bool = False
    len_beats: int = 0
    size_log2: int = 0


@dataclass(frozen=True)
class Transaction:
    """Reconstructed AXI transaction.

    Read txns are completed when RLAST arrives with the matching ID;
    write txns are completed when B arrives with the matching ID.
    """

    bundle_name: str
    is_read: bool
    txn_id: int
    addr: int
    len_beats: int
    size_log2: int
    t_start_fs: int
    t_first_data_fs: int
    t_end_fs: int
    resp: int


@dataclass
class ChannelStats:
    """Per-channel running stats for one bundle."""

    util_pct: float = 0.0
    bp_pct: float = 0.0
    peak_occ: int = 0
    txns: int = 0
    beats: int = 0


@dataclass
class LatencyStats:
    """Latency in cycles. p50/p95/p99 derived from the reservoir; hist_log2
    is filled at finalize time."""

    p50: int = 0
    p95: int = 0
    p99: int = 0
    max: int = 0
    hist_log2: list[int] = field(default_factory=lambda: [0] * 16)


@dataclass
class BundleStats:
    """Per-bundle accumulator. Mutable while the aggregate stage runs;
    serialized to the JSON v1 shape at emit time."""

    bundle: Bundle
    channels: dict[Channel, ChannelStats] = field(default_factory=dict)
    read_bps: float = 0.0
    write_bps: float = 0.0
    read_peak: int = 0
    read_avg: float = 0.0
    write_peak: int = 0
    write_avg: float = 0.0
    ar_to_r_first: LatencyStats = field(default_factory=LatencyStats)
    aw_to_b: LatencyStats = field(default_factory=LatencyStats)
    slverr: int = 0
    decerr: int = 0
    children: list["BundleStats"] = field(default_factory=list)


@dataclass
class InterconnectStats:
    """Per-interconnect-node roll-up. Computed after per-bundle aggregation."""

    node_path: str
    total_read_bps: float = 0.0
    total_write_bps: float = 0.0
    hottest_master: str = ""
    hottest_slave: str = ""
    fairness_jain: float = 1.0
    starved_masters: list[str] = field(default_factory=list)


@dataclass
class AggregateStats:
    """Top-level output of the aggregate stage; input to the emit stage."""

    design_top: str
    duration_cycles: int
    clock_period_ns: float
    bundles: list[BundleStats]
    interconnects: list[InterconnectStats]
