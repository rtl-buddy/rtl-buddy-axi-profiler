"""Unit tests for the SPA↔notebook sync client (Phase 3 of #16).

The full live path (real WebSocket → real broker) is exercised
end-to-end in rtl_buddy's hub test suite. Here we lock the
contract pieces this module actually owns:

* ``from_env()`` is inert without ``$RB_HUB_EVENTS_URL``.
* The on-message hook accepts only ``selection`` envelopes from
  non-self sources, and bumps the sequence number monotonically.
* ``publish_time_window`` queues a well-formed envelope; queue
  overflow drops cleanly.

We do not spin up the asyncio background thread for these tests —
the broker round-trip belongs to the rtl_buddy side. We poke the
internal hooks directly to keep this suite hermetic.
"""

from __future__ import annotations

import json
import threading

import pytest

from rtl_buddy_axi_profiler.notebook import sync as sync_mod


@pytest.fixture
def handle(monkeypatch):
    """Build an ``EventSync`` without actually starting the thread.

    The background thread tries to ``import websockets`` and connect
    to a URL — neither is available in CI for axi-profiler. We patch
    out ``_run_in_thread`` so the constructor returns immediately
    and the test exercises only the synchronous surface.
    """
    monkeypatch.setattr(sync_mod.EventSync, "_run_in_thread", lambda self: None)
    return sync_mod.EventSync(url="ws://test/api/events/sync")


def test_from_env_returns_none_without_url(monkeypatch):
    monkeypatch.delenv("RB_HUB_EVENTS_URL", raising=False)
    assert sync_mod.from_env() is None


def test_from_env_returns_none_for_blank_url(monkeypatch):
    monkeypatch.setenv("RB_HUB_EVENTS_URL", "   ")
    assert sync_mod.from_env() is None


def test_on_message_accepts_selection_and_bumps_seq(handle):
    seq0, payload0 = handle.latest_selection
    assert seq0 == 0 and payload0 is None

    handle._on_message(
        {
            "topic": "selection",
            "data": {"bundle": "axi_xbar", "test": "t1"},
            "source": "spa",
        }
    )
    seq1, payload1 = handle.latest_selection
    assert seq1 == 1
    assert payload1 == {"bundle": "axi_xbar", "test": "t1"}

    handle._on_message(
        {
            "topic": "selection",
            "data": {"bundle": "axi_other"},
            "source": "spa",
        }
    )
    seq2, payload2 = handle.latest_selection
    assert seq2 == 2
    assert payload2 == {"bundle": "axi_other"}


def test_on_message_rejects_self_source(handle):
    handle._on_message(
        {
            "topic": "selection",
            "data": {"bundle": "axi_xbar"},
            "source": "notebook",
        }
    )
    seq, payload = handle.latest_selection
    assert seq == 0
    assert payload is None


def test_on_message_ignores_unknown_topic(handle):
    handle._on_message({"topic": "noise", "data": {"x": 1}, "source": "spa"})
    assert handle.latest_selection == (0, None)


def test_publish_time_window_queues_envelope(handle):
    handle.publish_time_window(1000, 5000)
    raw = handle._outbound.get_nowait()
    env = json.loads(raw)
    assert env == {
        "topic": "time-window",
        "data": {"t_start_fs": 1000, "t_end_fs": 5000},
        "source": "notebook",
    }


def test_publish_time_window_drops_when_queue_full(handle):
    for i in range(sync_mod._OUTBOUND_MAX + 5):
        handle.publish_time_window(i, i + 1)
    # Drain — should have at most _OUTBOUND_MAX entries.
    drained = []
    while True:
        try:
            drained.append(handle._outbound.get_nowait())
        except Exception:
            break
    assert len(drained) == sync_mod._OUTBOUND_MAX


# --- inbound push hook (axi-profiler #46) --------------------------------


@pytest.fixture
def pushes(monkeypatch):
    """An ``EventSync`` whose ``on_inbound`` records each push call."""
    monkeypatch.setattr(sync_mod.EventSync, "_run_in_thread", lambda self: None)
    calls: list[int] = []
    handle = sync_mod.EventSync(
        url="ws://test/api/events/sync",
        on_inbound=lambda: calls.append(1),
    )
    return handle, calls


def test_on_message_fires_push_only_for_accepted_selection(pushes):
    handle, calls = pushes
    handle._on_message(
        {"topic": "selection", "data": {"bundle": "axi_xbar"}, "source": "spa"}
    )
    assert len(calls) == 1
    # Self-source and unknown-topic envelopes are dropped before the
    # push fires — the seq stays put and no re-execution is nudged.
    handle._on_message({"topic": "selection", "data": {"x": 1}, "source": "notebook"})
    handle._on_message({"topic": "noise", "data": {}, "source": "spa"})
    assert len(calls) == 1


def test_on_message_swallows_push_exception(monkeypatch):
    """A failing push must not break message handling — the poll
    backstop still carries the selection."""
    monkeypatch.setattr(sync_mod.EventSync, "_run_in_thread", lambda self: None)

    def boom() -> None:
        raise RuntimeError("setter blew up")

    handle = sync_mod.EventSync(url="ws://test/x", on_inbound=boom)
    handle._on_message({"topic": "selection", "data": {"bundle": "b"}, "source": "spa"})
    # State still updated despite the push raising.
    seq, payload = handle.latest_selection
    assert seq == 1 and payload == {"bundle": "b"}


def test_from_env_passes_on_inbound(monkeypatch):
    monkeypatch.setenv("RB_HUB_EVENTS_URL", "ws://test/api/events/sync")
    monkeypatch.setattr(sync_mod.EventSync, "_run_in_thread", lambda self: None)
    sentinel = object()
    captured: dict[str, object] = {}
    real_init = sync_mod.EventSync.__init__

    def spy_init(self, url, on_inbound=None):
        captured["on_inbound"] = on_inbound
        real_init(self, url, on_inbound=on_inbound)

    monkeypatch.setattr(sync_mod.EventSync, "__init__", spy_init)
    sync_mod.from_env(on_inbound=sentinel)  # type: ignore[arg-type]
    assert captured["on_inbound"] is sentinel


def test_spawn_thread_falls_back_without_kernel_context():
    """No marimo kernel context (CI / run-mode) → a plain daemon
    thread, not a crash. The push then no-ops and the poll carries on."""
    t = sync_mod._spawn_thread(lambda: None, "event-sync-test")
    assert isinstance(t, threading.Thread)
    assert t.daemon is True
    assert t.name == "event-sync-test"
    assert not t.is_alive()  # not started by the helper
