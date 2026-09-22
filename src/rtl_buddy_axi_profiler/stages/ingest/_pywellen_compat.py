"""Loud guard for the pywellen >=0.25 random-access Waveform API.

pywellen is pre-1.0 and rewrites its public surface on minor bumps:
0.25.0 replaced the 0.20-0.24 random-access API the ingest was built
on (``Waveform.hierarchy`` / ``get_signal_from_path`` /
``Signal.all_changes`` / ``value_at_time`` / ``value_at_idx``) with the
current ``wf[path]`` / ``Var.signal`` / ``Signal.value_at`` / ``sig[:]``
one. That break landed as a blanket "signal ... not found in trace"
because the lookup's ``except Exception`` swallowed the
``AttributeError`` — a dependency break reading as a user manifest
error (#52).

``pyproject.toml`` bounds the dependency to :data:`SUPPORTED_SPECIFIER`,
but that doesn't protect an environment that force-resolved an
out-of-range pywellen (a stale venv still pinning <0.25, or the next
pre-1.0 rewrite). :func:`require_random_access_api` turns that into a
clear error naming pywellen, its version and the supported range,
raised *before* the first Waveform touch — never an AttributeError
mid-walk, never a bogus "not found in trace".

``tests/test_pywellen_api.py`` is the CI-time half of the same guard:
it asserts this surface against a real VCD (no mocks — a mock would
happily model an API pywellen no longer has) and that the pyproject
pin still matches :data:`SUPPORTED_SPECIFIER`. Keep the three in step.

Deliberately *not* the streaming API (``stream_changes`` /
``WaveformStream``): it panicked with an index-out-of-bounds on the
all-signals, last-signal and single-signal cases through 0.25.2, and
the per-posedge sampler wants random access anyway.
"""

from __future__ import annotations

from importlib import metadata

import pywellen

#: Supported pywellen range, mirroring the pyproject pin. The floor is
#: the release carrying the upstream streaming fix plus its follow-up
#: patches (and the version rtl_buddy pins, so the two tools stay
#: co-installable — rtl_buddy#268); the cap stops the next pre-1.0
#: rewrite reaching the field (#52, #59).
MIN_VERSION = "0.25.6"
MAX_VERSION_EXCLUSIVE = "0.26"
SUPPORTED_SPECIFIER = f">={MIN_VERSION},<{MAX_VERSION_EXCLUSIVE}"

#: The pywellen surface the ingest calls, by class name: ``wf[path]``
#: lookup + var enumeration + timescale on Waveform, the zero-arg
#: getters on Var (all of these took a hierarchy argument before 0.25),
#: and the point query plus change-vector slicing on Signal.
REQUIRED_API: dict[str, tuple[str, ...]] = {
    "Waveform": ("__getitem__", "all_vars", "timescale"),
    "Var": ("signal", "name", "full_name", "bitwidth"),
    "Signal": ("value_at", "__getitem__", "__len__"),
}


class PywellenApiError(RuntimeError):
    """The installed pywellen can't drive the ingest's Waveform API."""


def pywellen_version() -> str:
    """Return the installed pywellen distribution version, or "unknown"."""
    try:
        return metadata.version("pywellen")
    except metadata.PackageNotFoundError:
        return "unknown"


def missing_api(module: object) -> list[str]:
    """Return the ``Class.attr`` names of :data:`REQUIRED_API` *module* lacks.

    A class missing outright is reported as bare ``Class`` — that's the
    shape a wholesale rewrite takes, and listing the attributes of a
    type that isn't there would be noise.
    """
    missing: list[str] = []
    for cls_name, attrs in REQUIRED_API.items():
        cls = getattr(module, cls_name, None)
        if cls is None:
            missing.append(cls_name)
            continue
        missing += [f"{cls_name}.{a}" for a in attrs if not hasattr(cls, a)]
    return missing


def require_random_access_api() -> None:
    """Raise :class:`PywellenApiError` unless pywellen has the 0.25 API.

    Called once at the top of every ingest entry point, before any
    Waveform is opened.
    """
    missing = missing_api(pywellen)
    if not missing:
        return
    version = pywellen_version()
    raise PywellenApiError(
        f"pywellen {version} lacks the random-access Waveform API the "
        f"wellen ingest requires (missing: {', '.join(missing)}; the "
        f"current surface arrived in pywellen 0.25.0) — reinstall with "
        f"'pywellen{SUPPORTED_SPECIFIER}' (rtl-buddy-axi-profiler#59). "
        f"This is a dependency problem, not a manifest problem."
    )


def lookup_signal(waveform: pywellen.Waveform, path: str) -> pywellen.Signal | None:
    """Resolve ``path`` to a :class:`pywellen.Signal`, or ``None`` on a miss.

    ``wf[path]`` is the >=0.25 lookup. It raises ``KeyError`` — and only
    ``KeyError`` — for a path the trace doesn't carry, so that is the
    single exception treated as "not in the trace"; anything else (the
    ``AttributeError`` an incompatible pywellen would raise, a reader
    panic) propagates loudly instead of masquerading as a bad manifest
    path (#52).

    A path that names a *scope* rather than a var resolves to a
    ``Scope``, which has no ``.signal``; that's a manifest error too, so
    it reads as a miss rather than an AttributeError.
    """
    try:
        var = waveform[path]
    except KeyError:
        return None
    if not isinstance(var, pywellen.Var):
        return None
    return var.signal


def preedge_time(tick: int) -> int:
    """The sample time whose value the design's flops latch at ``tick``.

    Trace times are integer ticks, so ``tick - 1`` is strictly before the
    edge and at or after the previous change — i.e. exactly the steady
    (setup) value, which is what ``Signal.value_at`` then returns.
    Sampling at ``tick`` itself would read the *post*-edge value and miss
    a single-cycle handshake whose READY deasserts as the transfer
    completes (#56). Clamped at 0: ``value_at`` takes an unsigned time.
    """
    return tick - 1 if tick > 0 else 0
