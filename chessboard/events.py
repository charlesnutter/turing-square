"""One queue, several producers.

Blocking on a single source is what made moves played in a browser invisible:
while the terminal waited for a typed line, nothing was reading the Lichess
stream, so the move arrived at a socket nobody was listening to.

Every source now runs in its own thread and posts to a shared queue. The main
loop consumes that queue and never blocks on any one source.

Phase 2 adds a serial-board producer as a third thread; the loop does not change.
"""

import queue
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

ITEM = "item"
EOF = "eof"
ERROR = "error"


@dataclass(frozen=True)
class Event:
    source: str
    kind: str = ITEM
    payload: Any = None


class EventBus:
    """Fan several blocking iterables into one queue.

    Producers are daemon threads: a blocked `readline` or a socket read must
    never keep the process alive once the game is over.
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[Event]" = queue.Queue()
        self._stopping = threading.Event()
        self._threads: list = []

    def add(self, source: str, iterable: Iterable) -> threading.Thread:
        def pump() -> None:
            try:
                for item in iterable:
                    if self._stopping.is_set():
                        return
                    self._queue.put(Event(source, ITEM, item))
                self._queue.put(Event(source, EOF))
            except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
                if not self._stopping.is_set():
                    self._queue.put(Event(source, ERROR, exc))

        thread = threading.Thread(target=pump, name=f"bus-{source}", daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    def post(self, source: str, payload: Any, kind: str = ITEM) -> None:
        self._queue.put(Event(source, kind, payload))

    def get(self, timeout: Optional[float] = None) -> Optional[Event]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stopping.set()
