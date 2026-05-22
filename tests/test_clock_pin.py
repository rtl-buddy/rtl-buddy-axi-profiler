"""Tests for the clock-pin detector heuristic (#3 follow-up)."""

from __future__ import annotations

from rtl_buddy_axi_profiler.stages.discover._clock_pin import detect_clock_port
from rtl_buddy_axi_profiler.stages.discover._summary import (
    ModuleSummary,
    PortSummary,
)


def _module(*ports: tuple[str, str]) -> ModuleSummary:
    """Test helper: build a ModuleSummary from (name, direction) pairs."""
    return ModuleSummary(
        name="m",
        file="m.sv",
        ports=tuple(
            PortSummary(name=name, direction=direction, width=1)
            for name, direction in ports
        ),
        instances=(),
    )


def test_picks_plain_clk_when_only_option() -> None:
    m = _module(("clk", "input"), ("data", "input"))
    assert detect_clock_port(m) == "clk"


def test_picks_aclk_over_clk() -> None:
    """Per AXI naming convention, ``aclk`` is preferred over ``clk``."""
    m = _module(("clk", "input"), ("aclk", "input"))
    assert detect_clock_port(m) == "aclk"


def test_picks_bundle_prefixed_clock_over_generic_when_prefix_set() -> None:
    """With ``m_axi`` prefix, ``m_axi_aclk`` wins over generic ``clk``."""
    m = _module(
        ("clk", "input"),
        ("m_axi_aclk", "input"),
        ("s_axi_aclk", "input"),
    )
    assert detect_clock_port(m, prefix="m_axi_") == "m_axi_aclk"


def test_picks_generic_aclk_when_prefix_doesnt_match() -> None:
    """If the prefixed clock doesn't exist, a generic aclk still wins."""
    m = _module(("clk", "input"), ("aclk", "input"))
    assert detect_clock_port(m, prefix="m_axi_") == "aclk"


def test_returns_none_when_no_clock_like_port() -> None:
    m = _module(("data", "input"), ("valid", "output"))
    assert detect_clock_port(m) is None


def test_picks_suffix_pattern_clk() -> None:
    """``my_clk`` style names also match the suffix rule."""
    m = _module(("data", "input"), ("my_clk", "input"))
    assert detect_clock_port(m) == "my_clk"


def test_case_insensitive() -> None:
    m = _module(("CLK", "input"), ("data", "input"))
    assert detect_clock_port(m) == "CLK"


def test_deterministic_tie_break_on_alphabetical() -> None:
    """When two equally-good clocks exist, the alphabetically-first
    name wins so the choice is deterministic."""
    m = _module(("zclk", "input"), ("aclk", "input"))
    assert detect_clock_port(m) == "aclk"
