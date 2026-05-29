"""Optional WebSocket sync client for the notebook template.

Phase 3 of the marimo umbrella (axi-profiler #16): joins the
rtl-buddy-hub event broker at ``$RB_HUB_EVENTS_URL`` so a bundle
click in the SPA reaches the notebook.

This speaks the real hub wire contract (``hub-protocol-v1.json``,
rtl-buddy-axi-profiler#48): on connect it sends a ``hello`` request as
``origin=notebook`` (the hub drops un-greeted peers), then consumes
``selection_changed`` events — ``{type, payload:{instance_path}, origin}``
— and exposes the latest one via :attr:`EventSync.latest_selection`.
The notebook→SPA direction (a brush publishing a time window) has no
hub message type yet, so :meth:`publish_time_window` is a no-op pending
that follow-up.

The notebook runs on its own loop (marimo / ASGI). We don't want to
fight with that loop, so the sync runs in a background thread with
its own ``asyncio`` event loop.

Two paths carry an inbound selection from that thread into marimo's
reactive graph (axi-profiler #46):

* **Push (low-latency).** When the thread is spawned during cell
  execution under a marimo *kernel* (edit mode), we use ``mo.Thread``
  instead of a raw ``threading.Thread``. ``mo.Thread`` propagates the
  kernel ``RuntimeContext`` to the worker, which is the supported
  contract that lets a cross-thread ``mo.state`` setter actually
  trigger cell re-execution. The ``on_inbound`` callback (a state
  setter wired by the template) fires the moment a ``selection``
  envelope arrives, so the notebook re-runs at arrival rather than on
  the next poll tick.
* **Poll (always-correct backstop).** Cells also read
  ``EventSync.latest_selection`` on a periodic ``mo.ui.refresh``. When
  there is no kernel context — run-mode, tests, or marimo not
  importable — the push setter is a silent no-op (``SetFunctor``
  swallows ``ContextNotInitializedError``) and the poll still delivers
  the selection. The poll is the load-bearing correctness guarantee;
  the push is a latency optimisation layered on top, never a
  dependency the bridge breaks without.

When ``$RB_HUB_EVENTS_URL`` is unset, ``from_env()`` returns ``None``
and the template degrades to standalone behaviour — every cell that
consumes the sync handle gates on ``None``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)


# rtl-buddy-hub wire contract (hub-protocol-v1.json). The notebook
# registers as its own ``Origin`` (rtl_buddy hub + rtl-buddy-axi-profiler#48)
# so SPA-origin ``selection_changed`` broadcasts reach it without
# colliding with ``view`` (the SPA) or ``cli`` (``rb hub send``).
_ORIGIN = "notebook"
_PROTOCOL_V = 1

try:
    from importlib.metadata import version as _pkg_version

    _CLIENT_VERSION = _pkg_version("rtl-buddy-axi-profiler")
except Exception:  # pragma: no cover - version lookup is best-effort
    _CLIENT_VERSION = "0+unknown"

_RECONNECT_INITIAL = 0.5
_RECONNECT_MAX = 8.0
_RECONNECT_FACTOR = 1.8


class EventSync:
    """Thread-safe handle to the hub event broker.

    All public methods are callable from marimo cells (main loop) or
    from the background WS thread; the only shared state is guarded
    by ``self._lock``.
    """

    def __init__(self, url: str, on_inbound: Callable[[], None] | None = None) -> None:
        self.url = url
        # Called (best-effort, from the WS thread) whenever a new
        # ``selection`` arrives — the template wires a ``mo.state``
        # setter here to push a re-execution. ``None`` falls back to
        # poll-only behaviour.
        self._on_inbound = on_inbound
        self._latest_selection: dict[str, Any] | None = None
        # Monotonic sequence — cells use this to detect "new selection
        # since I last looked" without holding the lock across an
        # equality check on the (mutable) dict payload.
        self._latest_selection_seq: int = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = _spawn_thread(self._run_in_thread, "event-sync")
        self._thread.start()

    @property
    def latest_selection(self) -> tuple[int, dict[str, Any] | None]:
        """Return ``(seq, payload)`` for the most recent inbound selection.

        ``seq`` increases by 1 every time a ``selection_changed`` event
        arrives — track it across calls to detect changes. The payload
        is ``{"instance_path": <str|list>, "origin": <str>}`` lifted
        from the hub envelope; the template maps ``instance_path`` to a
        bundle via the parquet's ``slave_path``.
        """
        with self._lock:
            return self._latest_selection_seq, self._latest_selection

    def publish_time_window(self, t_start_fs: int, t_end_fs: int) -> None:
        """No-op against the hub (kept so the #32 brush cell calls it
        harmlessly).

        There is no hub message type for a notebook→SPA time window yet:
        ``cursor_time_changed`` is a point, ``wave_zoom_to_range`` targets
        surfer, not the SPA's header chip. Wiring this needs a new hub
        event type + an SPA consumer — tracked as the outbound follow-up
        to rtl-buddy-axi-profiler#48. Emitting the old ``{topic:...}``
        envelope here would fail hub schema validation and risk dropping
        the peer, so we deliberately send nothing."""
        logger.debug(
            "publish_time_window is a no-op pending a hub time-window type"
            " (t_start_fs=%s t_end_fs=%s)",
            t_start_fs,
            t_end_fs,
        )

    def close(self) -> None:
        """Signal the background thread to exit (best-effort)."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _hello(self) -> str:
        """The hub requires a ``hello`` request as the first message
        before it registers the peer and delivers any broadcasts."""
        return json.dumps(
            {
                "v": _PROTOCOL_V,
                "id": str(uuid.uuid4()),
                "origin": _ORIGIN,
                "kind": "request",
                "type": "hello",
                "payload": {
                    "client": _ORIGIN,
                    "version": _CLIENT_VERSION,
                    "capabilities": [],
                },
            }
        )

    def _on_message(self, env: dict[str, Any]) -> None:
        # Defensive: ignore anything the hub echoes back with our own
        # origin. Everything that isn't a selection (welcome, peer_joined,
        # cursor/scope events, …) is silently dropped.
        if env.get("origin") == _ORIGIN or env.get("type") != "selection_changed":
            return
        payload = env.get("payload") or {}
        instance_path = payload.get("instance_path")
        if not instance_path:
            return
        with self._lock:
            self._latest_selection = {
                "instance_path": instance_path,
                "origin": env.get("origin"),
            }
            self._latest_selection_seq += 1
        # Push outside the lock: nudge marimo to re-run the consuming
        # cell now rather than on the next poll tick. No-op when there's
        # no kernel context (run-mode / plain thread); the poll backstop
        # still carries the selection.
        if self._on_inbound is not None:
            try:
                self._on_inbound()
            except Exception:
                logger.debug("event-sync push failed", exc_info=True)

    def _run_in_thread(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            logger.warning("event-sync background loop exited: %s", exc)

    async def _main(self) -> None:
        try:
            import websockets  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("event-sync disabled: install `websockets` package")
            return

        delay = _RECONNECT_INITIAL
        while not self._should_stop():
            try:
                async with websockets.connect(self.url) as ws:
                    delay = _RECONNECT_INITIAL
                    # Register first; the hub drops un-greeted peers.
                    await ws.send(self._hello())
                    await self._reader(ws)
            except Exception as exc:
                logger.debug("event-sync reconnect after %s", exc)
            if self._should_stop():
                return
            await asyncio.sleep(delay)
            delay = min(delay * _RECONNECT_FACTOR, _RECONNECT_MAX)

    def _should_stop(self) -> bool:
        """True once close() is called or the spawning marimo cell is
        invalidated (re-run / deleted / interrupted).

        The second clause only fires when we're on an ``mo.Thread``;
        ``current_thread()`` raises on a plain thread, which we treat
        as "no marimo-driven stop signal"."""
        if self._stop.is_set():
            return True
        try:
            from marimo import current_thread

            return bool(current_thread().should_exit)
        except Exception:
            return False

    async def _reader(self, ws: Any) -> None:
        async for msg in ws:
            try:
                env = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if isinstance(env, dict):
                self._on_message(env)


def _spawn_thread(target: Callable[[], None], name: str) -> threading.Thread:
    """Spawn the background WS thread.

    Prefer ``mo.Thread`` when a marimo kernel runtime context is
    installed: it propagates that context to the worker, which is the
    supported contract that lets the cross-thread ``mo.state`` push
    (``on_inbound``) trigger cell re-execution. Fall back to a plain
    daemon thread when marimo isn't importable (this module has no hard
    marimo dependency) or no kernel context exists (run-mode, tests) —
    there the push silently no-ops and the poll backstop carries the
    selection through. The probe touches a marimo-internal symbol, so
    any failure degrades to the plain thread rather than propagating.
    """
    try:
        from marimo import Thread as _MoThread
        from marimo._runtime.context.types import runtime_context_installed

        if runtime_context_installed():
            return _MoThread(target=target, name=name, daemon=True)
    except Exception:
        pass
    return threading.Thread(target=target, name=name, daemon=True)


def from_env(on_inbound: Callable[[], None] | None = None) -> EventSync | None:
    """Construct an ``EventSync`` from ``$RB_HUB_EVENTS_URL`` or return None.

    Used by the notebook template's first cell. ``on_inbound`` is the
    optional push hook (a ``mo.state`` setter) fired when a selection
    arrives; omit it for poll-only behaviour. Failures during thread
    startup are swallowed and logged — a missing broker must not break
    the notebook's standalone path.
    """
    url = os.environ.get("RB_HUB_EVENTS_URL", "").strip()
    if not url:
        return None
    try:
        return EventSync(url, on_inbound=on_inbound)
    except Exception as exc:
        logger.warning("event-sync init failed: %s", exc)
        return None
