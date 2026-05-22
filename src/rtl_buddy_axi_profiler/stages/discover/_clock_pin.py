"""Heuristic clock-pin detector for AXI bundles.

Each AXI interface has its own clock pin on the master module's
port list. This module picks the most-likely clock signal for a
given module:

1. Bundle-prefixed pattern: ``<bundle_prefix>aclk`` or
   ``<bundle_prefix>clk`` matching the same prefix as the AXI
   signal group (e.g. ``m_axi_aclk`` for the ``m_axi`` group).
2. Generic clock pin names: ``aclk``, ``clk``, ``clock`` (in that
   priority).
3. Anything matching ``*_clk`` / ``*_clock``.

The detector returns the canonical signal path (instance_path +
port name) so the ingest stage's wellen lookup uses it directly.
"""

from __future__ import annotations

import re

from rtl_buddy_axi_profiler.stages.discover._summary import ModuleSummary

# Order matters: higher = preferred. Each entry is (regex, priority).
# Priorities sort descending; ties broken by alphabetical port name
# so the choice is deterministic across runs.
_CLOCK_NAME_RULES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^aclk$", re.IGNORECASE), 100),
    (re.compile(r"^clk$", re.IGNORECASE), 90),
    (re.compile(r"^clock$", re.IGNORECASE), 85),
    (re.compile(r".*_aclk$", re.IGNORECASE), 70),
    (re.compile(r".*_clk$", re.IGNORECASE), 60),
    (re.compile(r".*_clock$", re.IGNORECASE), 50),
)


def detect_clock_port(module: ModuleSummary, *, prefix: str = "") -> str | None:
    """Return the best-match clock-pin name on ``module``, or None.

    ``prefix`` is the AXI signal prefix (e.g. ``m_axi_``) — when
    set, port names matching ``{prefix}aclk`` / ``{prefix}clk`` get
    a priority bump so a bundle's own clock wins over a generic
    ``clk`` shared with other interfaces.
    """
    candidates: list[tuple[int, str]] = []
    for port in module.ports:
        for pattern, priority in _CLOCK_NAME_RULES:
            if pattern.match(port.name):
                bumped = priority
                if prefix:
                    plain = port.name.lower()
                    if plain.startswith(prefix.lower()):
                        bumped += 50  # bundle-prefixed clock wins
                candidates.append((bumped, port.name))
                break
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], c[1]))
    return candidates[0][1]
