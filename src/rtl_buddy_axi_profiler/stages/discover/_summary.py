"""In-memory shape the detectors operate on.

Decoupled from how the SV was parsed: a CST-based builder, a
regex-based builder, or a hand-crafted fixture can all produce a
:class:`DesignSummary`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PortSummary:
    """One module port. ``width`` is in bits; 0 means unknown
    (parametric or vector-of-vectors).
    """

    name: str
    direction: str  # "input" | "output" | "inout"
    width: int = 1


@dataclass(frozen=True)
class PortConnection:
    """One entry in an instantiation's port map: ``.port_name(net)``."""

    port_name: str
    net: str


@dataclass(frozen=True)
class InstanceSummary:
    """A child instantiation inside a parent module."""

    name: str
    module: str
    parent_module: str
    connections: tuple[PortConnection, ...] = ()


@dataclass(frozen=True)
class ModuleSummary:
    name: str
    file: str
    ports: tuple[PortSummary, ...] = ()
    instances: tuple[InstanceSummary, ...] = ()


@dataclass
class DesignSummary:
    """Aggregate of every module parsed across a filelist."""

    top: str
    modules: dict[str, ModuleSummary] = field(default_factory=dict)

    def add(self, module: ModuleSummary) -> None:
        self.modules[module.name] = module

    def instance_paths_of(self, module_name: str) -> list[str]:
        """Walk the instance tree from ``top``; return every path where
        an instance has type ``module_name``.

        Paths look like ``top.u_a.u_b``. The top is included as the
        first path segment.
        """
        results: list[str] = []
        self._walk(self.top, [self.top], module_name, results)
        return results

    def _walk(
        self,
        current_module: str,
        path_segments: list[str],
        target_module: str,
        out: list[str],
    ) -> None:
        module = self.modules.get(current_module)
        if module is None:
            return
        for inst in module.instances:
            new_path = path_segments + [inst.name]
            if inst.module == target_module:
                out.append(".".join(new_path))
            # Recurse — designs can nest target_module under other types.
            self._walk(inst.module, new_path, target_module, out)
