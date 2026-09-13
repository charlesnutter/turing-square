"""The event bus: several blocking sources, one queue, nothing starved."""

import threading
import time

from chessboard.events import EOF, ERROR, ITEM, EventBus


def drain(bus, expected):
    out = []
    while len(out) < expected:
        event = bus.get(timeout=2.0)
        assert event is not None, f"timed out with {out}"
        out.append(event)
    return out


def test_an_iterable_is_drained_then_reports_eof():
    bus = EventBus()
    bus.add("nums", iter([1, 2, 3]))
    events = drain(bus, 4)
    assert [e.payload for e in events[:3]] == [1, 2, 3]
    assert all(e.kind == ITEM for e in events[:3])
    assert events[3].kind == EOF


def test_a_raising_producer_reports_the_error_rather_than_dying_quietly():
    def boom():
        yield "first"
        raise RuntimeError("stream died")

    bus = EventBus()
    bus.add("s", boom())
    events = drain(bus, 2)
    assert events[0].payload == "first"
    assert events[1].kind == ERROR
    assert isinstance(events[1].payload, RuntimeError)


def test_a_slow_source_does_not_starve_a_fast_one():
    """The whole point: a blocked reader must not hold up another source."""
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(0.4)      # stands in for a socket with nothing on it
        yield "late"

    bus = EventBus()
    bus.add("slow", slow())
    started.wait(1.0)
    bus.add("fast", iter(["early"]))

    first = bus.get(timeout=1.0)
    assert first.source == "fast" and first.payload == "early"


def test_post_injects_events_directly():
    bus = EventBus()
    bus.post("x", "hello")
    bus.post("x", None, EOF)
    assert bus.get().payload == "hello"
    assert bus.get().kind == EOF


def test_get_returns_none_on_timeout_rather_than_raising():
    assert EventBus().get(timeout=0.01) is None
