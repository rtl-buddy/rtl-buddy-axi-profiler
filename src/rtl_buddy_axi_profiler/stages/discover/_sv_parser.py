"""Minimal regex-based SV parser.

Captures the subset the AXI bundle detectors need:
- module declarations + ANSI-style port lists
- module-body child instantiations + their port maps

NOT a general SV parser. Handles the kinds of fixtures shipped in
``tests/fixtures/discover/``; complex SV (generates, packages,
non-ANSI port lists, parameter expressions) is out of scope. A
Verible-CST-based builder is tracked as a follow-up issue.

Comments (`//`, `/* */`) are stripped before parsing; string
literals are not (the parser doesn't tolerate ``;`` or ``)`` inside
strings in port maps).
"""

from __future__ import annotations

import re
from pathlib import Path

from rtl_buddy_axi_profiler.stages.discover._summary import (
    DesignSummary,
    InstanceSummary,
    ModuleSummary,
    PortConnection,
    PortSummary,
)

_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

_MODULE_RE = re.compile(
    r"""
    \bmodule\s+(?P<name>\w+)
    (?:\s*\#\([^)]*\))?           # optional param block
    \s*(?:\((?P<ports>[^;]*?)\))? # optional ANSI port list
    \s*;
    (?P<body>.*?)                  # body
    \bendmodule\b
    """,
    re.DOTALL | re.VERBOSE,
)

# Port declarations inside the ANSI parens. Direction + optional
# type + optional packed dimensions + name(s).
_PORT_DECL_RE = re.compile(
    r"""
    (?P<dir>input|output|inout)\s+
    (?:wire|reg|logic|bit)?\s*
    (?:signed\s+)?
    (?:\[(?P<msb>[^\]]+)\s*:\s*(?P<lsb>[^\]]+)\])?\s*
    (?P<names>[\w,\s]+?)
    (?=,\s*(?:input|output|inout)\b|\Z)
    """,
    re.DOTALL | re.VERBOSE,
)

# Child instantiations inside a module body:
#   modtype inst_name (.port(net), ...);
# Conservative — only matches identifier-style instance names.
_INSTANCE_RE = re.compile(
    r"""
    (?:^|\n)\s*
    (?P<module>\w+)\s+
    (?:\#\([^)]*\)\s*)?      # optional param block on instance
    (?P<inst>\w+)
    \s*\(
    (?P<ports>[^;]*?)
    \)\s*;
    """,
    re.DOTALL | re.VERBOSE,
)

_PORT_MAP_RE = re.compile(r"\.(?P<port>\w+)\s*\(\s*(?P<net>[\w\[\]:'.]*)\s*\)")

# Keywords that look like instantiations but aren't.
_NOT_INSTANCE_KEYWORDS = frozenset(
    {
        "assign",
        "always",
        "always_ff",
        "always_comb",
        "always_latch",
        "initial",
        "final",
        "if",
        "else",
        "for",
        "while",
        "generate",
        "endgenerate",
        "begin",
        "end",
        "case",
        "endcase",
        "wire",
        "reg",
        "logic",
        "bit",
        "input",
        "output",
        "inout",
        "parameter",
        "localparam",
        "typedef",
        "module",
        "endmodule",
        "interface",
        "endinterface",
        "function",
        "endfunction",
        "task",
        "endtask",
        "import",
        "export",
        "return",
    }
)


def strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


def parse_files(files: list[Path], top: str) -> DesignSummary:
    """Parse every file in ``files`` and return a populated DesignSummary."""
    design = DesignSummary(top=top)
    for path in files:
        text = strip_comments(path.read_text())
        for module in _parse_modules(text, file=str(path)):
            design.add(module)
    return design


def _parse_modules(text: str, *, file: str) -> list[ModuleSummary]:
    out: list[ModuleSummary] = []
    for m in _MODULE_RE.finditer(text):
        name = m.group("name")
        ports_text = m.group("ports") or ""
        body = m.group("body") or ""
        ports = tuple(_parse_ports(ports_text))
        instances = tuple(_parse_instances(body, parent_module=name))
        out.append(
            ModuleSummary(name=name, file=file, ports=ports, instances=instances)
        )
    return out


def _parse_ports(ports_text: str) -> list[PortSummary]:
    if not ports_text.strip():
        return []
    # Pad with terminator to make the lookahead in the regex happy.
    padded = ports_text.strip() + ","
    results: list[PortSummary] = []
    for m in _PORT_DECL_RE.finditer(padded):
        direction = m.group("dir")
        names_raw = m.group("names")
        msb = m.group("msb")
        lsb = m.group("lsb")
        width = _resolve_width(msb, lsb)
        for raw in names_raw.split(","):
            name = raw.strip()
            if not name:
                continue
            results.append(PortSummary(name=name, direction=direction, width=width))
    return results


def _resolve_width(msb: str | None, lsb: str | None) -> int:
    if msb is None or lsb is None:
        return 1
    try:
        return int(msb.strip()) - int(lsb.strip()) + 1
    except ValueError:
        return 0  # parametric or non-numeric; unknown


def _parse_instances(body: str, *, parent_module: str) -> list[InstanceSummary]:
    out: list[InstanceSummary] = []
    for m in _INSTANCE_RE.finditer(body):
        module = m.group("module")
        if module in _NOT_INSTANCE_KEYWORDS:
            continue
        inst = m.group("inst")
        if inst in _NOT_INSTANCE_KEYWORDS:
            continue
        ports_text = m.group("ports") or ""
        connections = tuple(
            PortConnection(port_name=pm.group("port"), net=pm.group("net").strip())
            for pm in _PORT_MAP_RE.finditer(ports_text)
        )
        out.append(
            InstanceSummary(
                name=inst,
                module=module,
                parent_module=parent_module,
                connections=connections,
            )
        )
    return out
