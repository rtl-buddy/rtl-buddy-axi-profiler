"""Port-name regex detector for AXI bundles.

For every parsed module, groups its ports by AXI prefix (the part
before ``ar`` / ``aw`` / ``r`` / ``w`` / ``b``). A group with at
least valid + ready on all five channels is treated as one AXI
bundle the module sits on. Direction tells us whether the module
is the master or the slave for that bundle.

Endpoint inference: for a master ``M`` and a slave ``S`` instantiated
under the same parent, if ``M.{prefix}_arvalid`` and ``S.{prefix}_arvalid``
connect to the same net in the parent, they form a bundle. Otherwise
the slave endpoint is reported as needing user input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    DefaultView,
    Protocol,
)

from rtl_buddy_axi_profiler.stages.discover._clock_pin import detect_clock_port
from rtl_buddy_axi_profiler.stages.discover._summary import (
    DesignSummary,
    ModuleSummary,
)


# Canonical AXI4 handshake roles required for a port group to count
# as an AXI bundle. We don't require burst/id/resp here — those are
# captured opportunistically but missing in AXI-Lite, and the
# handshake pair on every channel is the minimum-viable signature.
_REQUIRED_ROLES = (
    "arvalid",
    "arready",
    "awvalid",
    "awready",
    "rvalid",
    "rready",
    "wvalid",
    "wready",
    "bvalid",
    "bready",
)

_PORT_ROLE_RE = re.compile(
    r"^(?P<prefix>.*?)(?P<role>ar(?:valid|ready|addr|len|size|id|burst|cache|prot|qos|region|user|lock)"
    r"|aw(?:valid|ready|addr|len|size|id|burst|cache|prot|qos|region|user|lock)"
    r"|r(?:valid|ready|data|id|resp|last|user)"
    r"|w(?:valid|ready|data|strb|last|user)"
    r"|b(?:valid|ready|id|resp|user))$",
    re.IGNORECASE,
)


@dataclass
class _ModuleBundle:
    """Intermediate detection result for one module's AXI port group."""

    module: ModuleSummary
    prefix: str
    role_to_port: dict[str, str]
    data_width: int
    id_width: int
    is_master: bool


def detect(design: DesignSummary) -> list[Bundle]:
    """Return discovered AXI bundles for ``design``.

    One Bundle per (master_instance, slave_instance) net-paired pair;
    when the slave can't be resolved, the bundle still emits with a
    placeholder slave_path and ``needs_user_input`` listing the
    missing field.
    """
    module_bundles: list[_ModuleBundle] = []
    for module in design.modules.values():
        module_bundles.extend(_detect_bundles_in_module(module))

    return _pair_endpoints(design, module_bundles)


def _detect_bundles_in_module(module: ModuleSummary) -> list[_ModuleBundle]:
    """Find AXI port-prefix groups in one module's port list."""
    # Bucket: prefix -> {role: PortSummary}
    by_prefix: dict[str, dict[str, str]] = {}
    by_prefix_dirs: dict[str, dict[str, str]] = {}
    by_prefix_widths: dict[str, dict[str, int]] = {}

    for port in module.ports:
        match = _PORT_ROLE_RE.match(port.name.lower())
        if not match:
            continue
        prefix = match.group("prefix") or ""
        role = match.group("role").lower()
        by_prefix.setdefault(prefix, {})[role] = port.name
        by_prefix_dirs.setdefault(prefix, {})[role] = port.direction
        by_prefix_widths.setdefault(prefix, {})[role] = port.width

    bundles: list[_ModuleBundle] = []
    for prefix, roles in by_prefix.items():
        if not all(role in roles for role in _REQUIRED_ROLES):
            continue

        dirs = by_prefix_dirs[prefix]
        # Master if its arvalid is an output; slave if input.
        is_master = dirs.get("arvalid") == "output"
        widths = by_prefix_widths[prefix]
        bundles.append(
            _ModuleBundle(
                module=module,
                prefix=prefix,
                role_to_port=roles,
                data_width=widths.get("rdata", 0) or widths.get("wdata", 0) or 0,
                id_width=widths.get("arid", 0) or widths.get("awid", 0) or 0,
                is_master=is_master,
            )
        )
    return bundles


def _pair_endpoints(
    design: DesignSummary, module_bundles: list[_ModuleBundle]
) -> list[Bundle]:
    """Pair masters to slaves by shared net connection in a common parent.

    For each master+slave with matching signal-net connections under the
    same parent module, emit one Bundle. Unmatched masters still emit a
    bundle entry with ``needs_user_input: [slave_path]``.
    """
    masters = [b for b in module_bundles if b.is_master]
    slaves = [b for b in module_bundles if not b.is_master]

    out: list[Bundle] = []
    used_slave_ids: set[int] = set()

    for master in masters:
        master_inst_paths = design.instance_paths_of(master.module.name)
        if not master_inst_paths:
            # Master is the top-level module itself.
            master_inst_paths = [design.top] if master.module.name == design.top else []
        if not master_inst_paths:
            continue

        for master_path in master_inst_paths:
            matched_slave = _find_net_paired_slave(
                design, master, master_path, slaves, used_slave_ids
            )
            out.extend(_emit_bundle_pair(master, master_path, matched_slave))
    return out


def _find_net_paired_slave(
    design: DesignSummary,
    master: _ModuleBundle,
    master_inst_path: str,
    slaves: list[_ModuleBundle],
    used_slave_ids: set[int],
) -> tuple[_ModuleBundle, str] | None:
    """Return (slave_bundle, slave_inst_path) or None.

    Algorithm: the parent of master_inst_path is the SV module that
    instantiates the master. Look at that parent's instances; for each
    OTHER instance whose module has a matching slave bundle, check
    whether their arvalid connections share the same net string.
    """
    segments = master_inst_path.split(".")
    if len(segments) < 2:
        return None
    parent_path = ".".join(segments[:-1])
    master_inst_name = segments[-1]

    parent_module_name = _module_at_path(design, parent_path)
    if parent_module_name is None:
        return None
    parent_module = design.modules.get(parent_module_name)
    if parent_module is None:
        return None

    master_inst = next(
        (i for i in parent_module.instances if i.name == master_inst_name), None
    )
    if master_inst is None:
        return None

    master_arvalid_port = master.role_to_port.get("arvalid")
    if master_arvalid_port is None:
        return None
    master_arvalid_net = _net_for_port(master_inst, master_arvalid_port)
    if master_arvalid_net is None:
        return None

    for sibling in parent_module.instances:
        if sibling.name == master_inst_name:
            continue
        for slave in slaves:
            if id(slave) in used_slave_ids:
                continue
            if slave.module.name != sibling.module:
                continue
            slave_arvalid_port = slave.role_to_port.get("arvalid")
            if slave_arvalid_port is None:
                continue
            net = _net_for_port(sibling, slave_arvalid_port)
            if net == master_arvalid_net:
                used_slave_ids.add(id(slave))
                slave_path = parent_path + "." + sibling.name
                return slave, slave_path
    return None


def _module_at_path(design: DesignSummary, path: str) -> str | None:
    """Resolve a dotted instance path to its module name."""
    parts = path.split(".")
    if parts[0] != design.top:
        return None
    current_module = design.top
    for part in parts[1:]:
        module = design.modules.get(current_module)
        if module is None:
            return None
        inst = next((i for i in module.instances if i.name == part), None)
        if inst is None:
            return None
        current_module = inst.module
    return current_module


def _net_for_port(instance, port_name: str) -> str | None:
    for conn in instance.connections:
        if conn.port_name == port_name:
            return conn.net
    return None


def _emit_bundle_pair(
    master: _ModuleBundle,
    master_path: str,
    matched: tuple[_ModuleBundle, str] | None,
) -> list[Bundle]:
    name = (
        f"{master_path.split('.')[-1]}_axi"
        if not master.prefix
        else (f"{master_path.split('.')[-1]}_{master.prefix.rstrip('_')}")
    )
    signals = _signals_for_role_map(master, master_path)
    clock = _detect_clock_signal(master, master_path)
    if matched is None:
        return [
            Bundle(
                name=name,
                master_path=master_path,
                slave_path="?",
                protocol=Protocol.AXI4,
                data_width=master.data_width or 0,
                id_width=master.id_width or 0,
                source=BundleSource.VERIBLE_REGEX,
                default_view=DefaultView.PARENT,
                signals=signals,
                clock_signal=clock,
            )
        ]
    slave, slave_path = matched
    return [
        Bundle(
            name=name,
            master_path=master_path,
            slave_path=slave_path,
            protocol=Protocol.AXI4,
            data_width=master.data_width or slave.data_width or 0,
            id_width=master.id_width or slave.id_width or 0,
            source=BundleSource.VERIBLE_REGEX,
            default_view=DefaultView.PARENT,
            # Canonical role → master-side signal path. The ingest
            # stage extracts from the master's port; nets are shared
            # so the slave side is redundant. (See axi-bundles-schema.md.)
            signals=signals,
            clock_signal=clock,
        )
    ]


def _detect_clock_signal(master: _ModuleBundle, master_path: str) -> str:
    """Resolve a bundle's clock-pin port to a fully qualified path."""
    clock_port = detect_clock_port(master.module, prefix=master.prefix)
    if clock_port is None:
        return ""
    return f"{master_path}.{clock_port}"


def _signals_for_role_map(bundle: _ModuleBundle, inst_path: str) -> dict[str, str]:
    """Map canonical roles (arvalid, arready, ...) to fully qualified
    signal paths on the bundle's owning instance."""
    return {
        role: f"{inst_path}.{port_name}"
        for role, port_name in bundle.role_to_port.items()
    }
