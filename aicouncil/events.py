"""A tiny in-process pub/sub bus that feeds the browser over Server-Sent Events.

Why SSE and not WebSockets: the whole app is stdlib-only, and the SSE wire
format is three lines of text over an ordinary HTTP response. Hand-rolling
RFC 6455 frame masking in ``http.server`` would be a lot of fragile code for a
stream that only ever flows server -> browser.

Each subscriber gets its own bounded queue. A subscriber that stops draining
(a closed laptop lid, a stalled tab) drops its oldest events rather than
growing without limit or blocking the pipeline thread.
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from typing import Any, Dict, Iterator, List

# Ring buffer of recent events so a browser that reconnects (or opens a second
# tab) can replay the run instead of seeing a blank screen.
REPLAY_LIMIT = 2000


class EventBus:
    """Fan-out event dispatcher with per-subscriber backpressure."""

    def __init__(self, replay_limit: int = REPLAY_LIMIT) -> None:
        self._lock = threading.RLock()
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._history: List[Dict[str, Any]] = []
        self._replay_limit = replay_limit
        self._seq = itertools.count(1)

    # -- publishing --------------------------------------------------------

    def publish(self, kind: str, **payload: Any) -> Dict[str, Any]:
        """Broadcast an event. Returns the fully-formed event dict."""
        event = {
            "id": next(self._seq),
            "kind": kind,
            "ts": time.time(),
            **payload,
        }
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._replay_limit:
                # Trim in one slice rather than popping per-event.
                del self._history[: len(self._history) - self._replay_limit]
            subscribers = list(self._subscribers)

        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow consumer: shed the oldest event and retry once. If the
                # queue is still full the consumer is gone; skip it silently.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        return event

    # -- subscribing -------------------------------------------------------

    def subscribe(self, replay_from: int = 0) -> "queue.Queue[Dict[str, Any]]":
        """Register a subscriber, pre-loaded with events after ``replay_from``."""
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=4096)
        with self._lock:
            if replay_from >= 0:
                for event in self._history:
                    if event["id"] > replay_from:
                        try:
                            q.put_nowait(event)
                        except queue.Full:
                            break
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # -- introspection -----------------------------------------------------

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)

    def clear(self) -> None:
        """Drop replay history. Called when a new run starts."""
        with self._lock:
            self._history.clear()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def sse_format(event: Dict[str, Any]) -> bytes:
    """Encode one event in the ``text/event-stream`` wire format.

    ``id:`` lets the browser's EventSource resume via Last-Event-ID, and the
    JSON payload goes on a single ``data:`` line (json.dumps never emits a raw
    newline, so no continuation handling is required).
    """
    body = json.dumps(event, default=str)
    return f"id: {event['id']}\nevent: {event['kind']}\ndata: {body}\n\n".encode("utf-8")


def sse_comment(text: str = "keepalive") -> bytes:
    """A no-op SSE comment, used as a heartbeat to hold proxies/tabs open."""
    return f": {text}\n\n".encode("utf-8")


def drain(q: "queue.Queue[Dict[str, Any]]", timeout: float) -> Iterator[Dict[str, Any]]:
    """Yield one event (blocking up to ``timeout``) then any others queued.

    Blocking for the first item keeps idle CPU at zero; draining the rest
    without blocking lets a burst of subprocess output flush in one write.
    """
    try:
        yield q.get(timeout=timeout)
    except queue.Empty:
        return
    while True:
        try:
            yield q.get_nowait()
        except queue.Empty:
            return
