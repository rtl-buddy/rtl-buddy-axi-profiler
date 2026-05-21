"""Tiny VCD writer used by the ingest tests.

Hand-rolled rather than depending on pyvcd — we only need to emit
simple value-change records for the test fixtures and the writer
fits in <100 lines. Generated VCDs are read back by pywellen.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _SigDecl:
    code: str
    name: str
    width: int


def _id_codes():
    """Yield printable ASCII codes for VCD signal ids.

    VCD ID codes are printable ASCII, 33-126 inclusive. One char
    gives 94 unique codes — enough for any AXI fixture we build
    by hand.
    """
    for code in range(33, 127):
        yield chr(code)


class VcdWriter:
    """Build a VCD piece by piece, then call ``render()``."""

    def __init__(self, *, timescale: str = "1ns") -> None:
        self._timescale = timescale
        self._signals: dict[str, _SigDecl] = {}
        self._scope_for: dict[str, str] = {}
        self._next_code = _id_codes()
        # (time, signal_path, value)
        self._events: list[tuple[int, str, int]] = []

    def declare(self, path: str, width: int) -> None:
        """Declare a signal under a hierarchical scope (dot-separated)."""
        if path in self._signals:
            return
        code = next(self._next_code)
        self._signals[path] = _SigDecl(code=code, name=path.split(".")[-1], width=width)
        self._scope_for[path] = ".".join(path.split(".")[:-1])

    def change(self, t: int, path: str, value: int) -> None:
        """Record a value change for ``path`` at time ``t``."""
        if path not in self._signals:
            raise ValueError(f"signal {path!r} not declared")
        self._events.append((t, path, value))

    def render(self) -> str:
        out: list[str] = []
        out.append(f"$timescale {self._timescale} $end")

        # Build the scope tree.
        scopes: dict[str, list[str]] = {}
        for path, scope in self._scope_for.items():
            scopes.setdefault(scope, []).append(path)

        # Emit scopes nested from the root down.
        rendered_scopes = set()

        def emit_scope(scope: str) -> None:
            if scope in rendered_scopes:
                return
            rendered_scopes.add(scope)
            name = scope.split(".")[-1] if scope else "top"
            out.append(f"$scope module {name} $end")
            # Recurse into child scopes.
            children = sorted(
                s
                for s in scopes
                if s.startswith(scope + ".")
                and s != scope
                and "." not in s[len(scope) + 1 :]
            )
            for child in children:
                emit_scope(child)
            # Emit vars in this scope.
            for path in sorted(scopes.get(scope, [])):
                sig = self._signals[path]
                kind = "wire"
                out.append(f"$var {kind} {sig.width} {sig.code} {sig.name} $end")
            out.append("$upscope $end")

        # Find root scopes (those with no parent or with parent ""):
        roots = sorted({s.split(".")[0] for s in self._scope_for.values()})
        for root in roots:
            emit_scope(root)
        out.append("$enddefinitions $end")

        # Sort events by time.
        self._events.sort(key=lambda ev: ev[0])
        last_t: int | None = None
        for t, path, value in self._events:
            if t != last_t:
                out.append(f"#{t}")
                last_t = t
            sig = self._signals[path]
            if sig.width == 1:
                out.append(f"{value}{sig.code}")
            else:
                bits = format(value, f"0{sig.width}b")
                out.append(f"b{bits} {sig.code}")
        return "\n".join(out) + "\n"
