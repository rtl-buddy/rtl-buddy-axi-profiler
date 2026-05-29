"""Optional WebSocket sync client for the notebook template.

Phase 3 of the marimo umbrella (axi-profiler #16): joins the
rtl-buddy-hub event broker at ``$RB_HUB_EVENTS_URL`` so a bundle
click in the SPA reaches the notebook, and a brush in the notebook
reaches the SPA's header chip.

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
from queue import Queue
from typing import Any, Callable

logger = logging.getLogger(__name__)


SOURCE = "notebook"
_RECONNECT_INITIAL = 0.5
_RECONNECT_MAX = 8.0
_RECONNECT_FACTOR = 1.8
_OUTBOUND_MAX = 64


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
        self._outbound: Queue[str] = Queue(maxsize=_OUTBOUND_MAX)
        self._stop = threading.Event()
        self._thread = _spawn_thread(self._run_in_thread, "event-sync")
        self._thread.start()

    @property
    def latest_selection(self) -> tuple[int, dict[str, Any] | None]:
        """Return ``(seq, payload)`` for the most recent inbound selection.

        ``seq`` increases by 1 every time a new ``selection`` envelope
        arrives — track it across calls to detect changes; the payload
        dict itself is the broker's ``data`` field.
        """
        with self._lock:
            return self._latest_selection_seq, self._latest_selection

    def publish_time_window(self, t_start_fs: int, t_end_fs: int) -> None:
        """Queue a ``time-window`` envelope for the next outbound flush."""
        env = {
            "topic": "time-window",
            "data": {"t_start_fs": int(t_start_fs), "t_end_fs": int(t_end_fs)},
            "source": SOURCE,
        }
        try:
            self._outbound.put_nowait(json.dumps(env))
        except Exception:
            # Queue full — drop. Time-window updates are throwaway;
            # the next user brush replaces this one.
            pass

    def close(self) -> None:
        """Signal the background thread to exit (best-effort)."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_message(self, env: dict[str, Any]) -> None:
        if env.get("source") == SOURCE:
            return
        topic = env.get("topic")
        if topic == "selection":
            with self._lock:
                self._latest_selection = env.get("data")
                self._latest_selection_seq += 1
            # Push outside the lock: nudge marimo to re-run the
            # consuming cell now rather than on the next poll tick.
            # No-op when there's no kernel context (run-mode / plain
            # thread); the poll backstop still carries the selection.
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
                    reader_task = asyncio.create_task(self._reader(ws))
                    writer_task = asyncio.create_task(self._writer(ws))
                    pending = (
                        await asyncio.wait(
                            [reader_task, writer_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    )[1]
                    for t in pending:
                        t.cancel()
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

    async def _writer(self, ws: Any) -> None:
        loop = asyncio.get_running_loop()
        while True:
            text = await loop.run_in_executor(None, self._outbound.get)
            await ws.send(text)


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
