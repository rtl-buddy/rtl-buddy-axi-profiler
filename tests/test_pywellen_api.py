"""Contract gate for the pywellen dependency (#52, #59).

pywellen is pre-1.0 and rewrites its public API on minor bumps: 0.25.0
removed the whole random-access ``Waveform`` surface 0.20-0.24 exposed,
which is what the wellen ingest was built on. With the unbounded pin
that break surfaced as "signal ... not found in trace" — a dependency
problem wearing a manifest problem's clothes (#52).

Three gates keep that from recurring, all of them loud rather than
skipped:

* the **API contract** — every attribute and call shape
  ``stages/ingest/wellen.py`` and ``stages/ingest/_clock_detect.py``
  use, exercised against a real VCD written by the test, never a mock.
  A mock would happily model an API pywellen no longer has, which is
  the exact failure being guarded.
* the **pin** — ``pyproject.toml`` must keep a two-sided pywellen
  requirement matching ``_pywellen_compat.SUPPORTED_SPECIFIER``, and
  the resolved version must satisfy it, so loosening the pin or a
  lockfile drifting out of range fails here instead of in the field.
* the **runtime guard** — ``require_random_access_api`` must actually
  fire on a pywellen missing the surface, with a message that names
  the version and the supported range.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest

import pywellen

from rtl_buddy_axi_profiler.stages.ingest import _pywellen_compat
from rtl_buddy_axi_profiler.stages.ingest._pywellen_compat import (
    PywellenApiError,
    lookup_signal,
    preedge_time,
)


_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

# Small enough to read at a glance, wide enough to pin every value shape
# the ingest meets: a 1-bit signal (valid/ready), a multi-bit bus (id /
# addr / len), a partially-z bus value, a signal whose first change is
# late (so there is a "before first change" time), an x state, and a
# nested scope.
_VCD = """\
$timescale 10ps $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 4 # bus $end
$scope module sub $end
$var wire 1 $ rst $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
b0000 #
#10
1!
b0011 #
x$
#20
0!
bzz11 #
1$
#30
1!
"""


@pytest.fixture
def vcd(tmp_path: Path) -> Path:
    path = tmp_path / "api.vcd"
    path.write_text(_VCD)
    return path


@pytest.fixture
def wf(vcd: Path):
    return pywellen.Waveform(str(vcd))


# ---------------------------------------------------------------------------
# API contract — real trace, no mocks
# ---------------------------------------------------------------------------


def test_required_api_surface_is_present() -> None:
    """The runtime guard's own table, checked against the real module.

    If ``REQUIRED_API`` drifts from what the installed pywellen offers,
    the guard passes and the ingest still dies mid-walk.
    """
    assert _pywellen_compat.missing_api(pywellen) == []


def test_waveform_path_lookup_returns_a_var(wf) -> None:
    var = wf["top.clk"]
    assert isinstance(var, pywellen.Var)
    assert var.name == "clk"
    assert var.full_name == "top.clk"


def test_waveform_path_lookup_misses_raise_key_error(wf) -> None:
    """``lookup_signal`` treats KeyError — and only KeyError — as a
    signal-not-in-the-dump miss; anything else is an API break and must
    propagate (#52)."""
    with pytest.raises(KeyError):
        wf["top.no_such_signal"]


def test_lookup_signal_maps_hit_miss_and_scope(wf) -> None:
    assert lookup_signal(wf, "top.clk") is not None
    assert lookup_signal(wf, "top.no_such_signal") is None
    # A scope path is a manifest error, not an API break: it reads as a
    # miss rather than an AttributeError on the absent ``.signal``.
    assert lookup_signal(wf, "top.sub") is None


def test_var_getters_are_zero_arg(wf) -> None:
    """All of these took a hierarchy argument before 0.25."""
    bus = wf["top.bus"]
    assert bus.name == "bus"
    assert bus.full_name == "top.bus"
    assert bus.bitwidth == 4
    assert bus.signal is not None


def test_all_vars_enumerates_the_flat_hierarchy(wf) -> None:
    """``detect_global_clock`` scans ``wf.all_vars()`` for 1-bit
    candidates; before 0.25 this was ``wf.hierarchy.all_vars()``."""
    assert [v.full_name for v in wf.all_vars()] == [
        "top.clk",
        "top.bus",
        "top.sub.rst",
    ]


def test_signal_value_at_change_and_between_changes(wf) -> None:
    """The per-posedge sampler reads at an arbitrary time, not only at
    edges — ``value_at`` must bisect and hold the previous value."""
    clk = wf["top.clk"].signal
    assert clk.value_at(0) == 0  # at the first change
    assert clk.value_at(5) == 0  # between changes: holds the previous value
    assert clk.value_at(10) == 1  # at a change
    assert clk.value_at(30) == 1  # at the last change
    assert clk.value_at(999) == 1  # after the last change: holds


def test_signal_value_before_first_change_is_none(wf) -> None:
    """``_to_int(None)`` -> 0, i.e. a signal that hasn't started yet
    reads deasserted rather than blowing up."""
    rst = wf["top.sub.rst"].signal
    assert rst.value_at(0) is None
    assert rst.value_at(9) is None
    assert rst.value_at(10) == "x"


def test_signal_change_vector_shape_and_value_types(wf) -> None:
    """``sig[:]`` is what ``_posedge_times`` walks — a time-ordered list
    of ``(int time, value)``, with an ``int`` value for a fully 2-state
    sample and a ``str`` when any bit is x or z (MSB first)."""
    clk = wf["top.clk"].signal
    assert clk[:] == [(0, 0), (10, 1), (20, 0), (30, 1)]
    assert len(clk) == 4

    bus = wf["top.bus"].signal
    assert bus[:] == [(0, 0), (10, 3), (20, "zz11")]
    assert isinstance(bus[:][1][1], int)  # multi-bit, 2-state -> int
    assert isinstance(bus[:][2][1], str)  # any x/z -> str

    rst = wf["top.sub.rst"].signal
    assert rst[:] == [(10, "x"), (20, 1)]


def test_timescale_is_a_property_with_factor_and_unit(wf) -> None:
    """``_tick_to_fs`` needs ``(factor, unit)``; before 0.25 this was
    ``wf.hierarchy.timescale()``."""
    ts = wf.timescale
    assert int(ts.factor) == 10
    assert str(ts.unit).lower() == "ps"


def test_timescale_is_none_when_the_trace_declares_none(tmp_path: Path) -> None:
    """The clock detector's "trace has no timescale" branch is reachable
    only if the absent timescale still reads as ``None`` under 0.25."""
    path = tmp_path / "no_ts.vcd"
    path.write_text(
        "$scope module top $end\n"
        "$var wire 1 ! clk $end\n"
        "$upscope $end\n"
        "$enddefinitions $end\n"
        "#0\n0!\n#5\n1!\n"
    )
    assert pywellen.Waveform(str(path)).timescale is None


def test_preedge_time_is_one_tick_back_and_clamps_at_zero(wf) -> None:
    """``value_at`` takes an unsigned time, so tick 0 must not go to -1.

    Sampling one tick before the edge reads the setup value the flops
    latch — the fix for the single-cycle handshake in #56, expressed
    against the 0.25 API (there is no ``value_at_idx`` any more).
    """
    assert preedge_time(20) == 19
    assert preedge_time(0) == 0
    clk = wf["top.clk"].signal
    assert clk.value_at(preedge_time(20)) == 1  # pre-edge: still high
    assert clk.value_at(20) == 0  # post-edge: the falling edge itself
    with pytest.raises(OverflowError):
        clk.value_at(-1)


# ---------------------------------------------------------------------------
# Runtime guard
# ---------------------------------------------------------------------------


def test_missing_api_names_the_gaps() -> None:
    """A wholesale rewrite reports the class; a partial one the attrs."""
    gutted = type("module", (), {"Waveform": type("Waveform", (), {})})
    missing = _pywellen_compat.missing_api(gutted)
    assert "Var" in missing and "Signal" in missing
    assert "Waveform.timescale" in missing


def test_require_random_access_api_raises_with_version_and_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message has to tell the user what to reinstall, and say this
    is a dependency problem — the whole point of #52."""
    monkeypatch.setattr(_pywellen_compat, "pywellen_version", lambda: "0.24.2")
    monkeypatch.setattr(_pywellen_compat, "missing_api", lambda _m: ["Waveform"])
    with pytest.raises(PywellenApiError) as excinfo:
        _pywellen_compat.require_random_access_api()
    message = str(excinfo.value)
    assert "0.24.2" in message
    assert _pywellen_compat.SUPPORTED_SPECIFIER in message


def test_require_random_access_api_passes_on_the_real_module() -> None:
    _pywellen_compat.require_random_access_api()


# ---------------------------------------------------------------------------
# Pin consistency
# ---------------------------------------------------------------------------


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def _pywellen_specifiers() -> list[tuple[str, str]]:
    """``[(operator, version), ...]`` from the pyproject pywellen pin.

    Parsed rather than string-compared: the invariant is "this floor,
    this cap", not the author's whitespace.
    """
    pyproject = tomllib.loads(_PYPROJECT.read_text())
    (req,) = [
        d for d in pyproject["project"]["dependencies"] if d.startswith("pywellen")
    ]
    out: list[tuple[str, str]] = []
    for clause in req[len("pywellen") :].split(","):
        clause = clause.strip()
        for op in (">=", "<=", "==", "!=", "<", ">", "~="):
            if clause.startswith(op):
                out.append((op, clause[len(op) :].strip()))
                break
    return out


def test_pyproject_pin_is_two_sided_and_matches_the_guard() -> None:
    """The cap is the root-cause fix for #52 — without it the next
    pre-1.0 rewrite resolves into a fresh install and breaks the ingest
    silently. The floor guarantees the 0.25 getter API (and keeps the
    profiler co-installable with rtl_buddy, which pins the same range —
    rtl-buddy-axi-profiler#59)."""
    specs = _pywellen_specifiers()
    floors = [v for op, v in specs if op == ">="]
    caps = [v for op, v in specs if op == "<"]
    assert floors and caps, (
        f"pywellen must keep a two-sided pin, got {specs} — an unbounded "
        "pin is what broke the ingest in #52"
    )
    (floor,) = floors
    (cap,) = caps
    assert floor == _pywellen_compat.MIN_VERSION
    assert cap == _pywellen_compat.MAX_VERSION_EXCLUSIVE


def test_installed_pywellen_satisfies_the_pin() -> None:
    """Catches a lockfile that drifted out of the declared range — the
    lock is what CI and every ``uv sync`` actually install."""
    version = _version_tuple(metadata.version("pywellen"))
    assert version >= _version_tuple(_pywellen_compat.MIN_VERSION)
    assert version < _version_tuple(_pywellen_compat.MAX_VERSION_EXCLUSIVE)
    assert _pywellen_compat.pywellen_version() == metadata.version("pywellen")
