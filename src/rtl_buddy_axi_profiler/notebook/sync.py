"""Optional WebSocket sync client for the notebook template.

Phase 3 of the marimo umbrella (axi-profiler #16): joins the
rtl-buddy-hub event broker at ``$RB_HUB_EVENTS_URL`` so a bundle
click in the SPA reaches the notebook, and a brush in the notebook
reaches the SPA's header chip.

The notebook runs on its own loop (marimo / ASGI). We don't want to
fight with that loop, so the sync runs in a background ``threading``
thread with its own ``asyncio`` event loop. Cells poll
``EventSync.latest_selection`` on a periodic refresh — cross-thread
``mo.state`` setters are a marimo-internal contract we'd rather not
rely on. 500 ms polling latency is fine for the human-driven
"click bundle in SPA, see it filter in notebook" UX.

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
from typing import Any

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

    def __init__(self, url: str) -> None:
        self.url = url
        self._latest_selection: dict[str, Any] | None = None
        # Monotonic sequence — cells use this to detect "new selection
        # since I last looked" without holding the lock across an
        # equality check on the (mutable) dict payload.
        self._latest_selection_seq: int = 0
        self._lock = threading.Lock()
        self._outbound: Queue[str] = Queue(maxsize=_OUTBOUND_MAX)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run_in_thread,
            daemon=True,
            name="event-sync",
        )
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

    def _run_in_thread(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            logger.warning("event-sync background loop exited: %s", exc)

    async def _main(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.warning("event-sync disabled: install `websockets` package")
            return

        delay = _RECONNECT_INITIAL
        while not self._stop.is_set():
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
            if self._stop.is_set():
                return
            await asyncio.sleep(delay)
            delay = min(delay * _RECONNECT_FACTOR, _RECONNECT_MAX)

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


def from_env() -> EventSync | None:
    """Construct an ``EventSync`` from ``$RB_HUB_EVENTS_URL`` or return None.

    Used by the notebook template's first cell. Failures during
    thread startup are swallowed and logged — a missing broker
    must not break the notebook's standalone path.
    """
    url = os.environ.get("RB_HUB_EVENTS_URL", "").strip()
    if not url:
        return None
    try:
        return EventSync(url)
    except Exception as exc:
        logger.warning("event-sync init failed: %s", exc)
        return None
