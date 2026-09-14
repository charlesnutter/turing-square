"""The move input boundary, as a producer.

A BoardInput no longer answers "what move now?" on demand -- it produces intent
whenever it has some, and the loop decides what that intent means against the
current board. That is what lets a keyboard, an engine and 64 magnets all feed
one loop without any of them blocking the others.
"""

import chess

from chessboard.drivers.board_input import BoardInput, KeyboardInput, read_move
from chessboard.events import EOF, EventBus


def test_a_keyboard_produces_the_lines_that_were_typed():
    assert list(KeyboardInput(["e4\n", "fen\n", "quit\n"])) == ["e4", "fen", "quit"]


def test_a_keyboard_stops_producing_when_the_input_ends():
    """Piped stdin running out is an ending, not a crash."""
    assert list(KeyboardInput([])) == []


def test_ctrl_c_ends_the_producer_rather_than_escaping_it():
    def interrupted():
        raise KeyboardInterrupt
        yield                       # pragma: no cover -- makes this a generator

    assert list(KeyboardInput(interrupted())) == []


def test_the_keyboard_never_writes_to_the_stream_it_reads():
    """It runs on a producer thread. Writing from there can deadlock stdout at
    interpreter shutdown, turning a clean quit into a fatal error."""
    written = []

    class Watched:
        def __iter__(self):
            return iter(["e4\n"])

        def write(self, text):      # pragma: no cover -- must never be called
            written.append(text)

    assert list(KeyboardInput(Watched())) == ["e4"]
    assert written == []


def test_a_keyboard_attaches_to_the_bus_under_its_own_name():
    bus = EventBus()
    keyboard = KeyboardInput(["d4\n"])
    bus.add(keyboard.name, keyboard)

    first = bus.get(timeout=2.0)
    assert (first.source, first.payload) == ("keyboard", "d4")
    assert bus.get(timeout=2.0).kind == EOF


def test_any_iterable_of_intent_satisfies_the_interface():
    """Phase 2's SerialBoardInput yields occupancy frames from this same base."""

    class FakeBoard(BoardInput):
        name = "board"

        def __iter__(self):
            yield 0xFFFF00000000FFFF

    frames = list(FakeBoard())
    assert frames == [0xFFFF00000000FFFF]


# --------------------------------------------------------------------------
# reading a move, and why it could not be read
# --------------------------------------------------------------------------

def test_a_legal_san_move_reads_with_no_complaint():
    move, reason = read_move(chess.Board(), "e4")
    assert move == chess.Move.from_uci("e2e4")
    assert reason is None


def test_uci_reads_even_when_the_move_is_illegal():
    """The core explains an illegal move far better than the parser can, so an
    unplayable UCI move must survive to reach it."""
    move, reason = read_move(chess.Board(), "e2e5")
    assert move == chess.Move.from_uci("e2e5")
    assert reason is None


def test_illegal_san_says_it_is_illegal_rather_than_unreadable():
    """`Qd5` is perfectly readable. Saying otherwise sends the player hunting
    for a typo in a move they spelled correctly."""
    move, reason = read_move(chess.Board(), "Qd5")
    assert move is None
    assert "not legal" in reason
    assert "cannot read" not in reason and "not a move I can read" not in reason


def test_ambiguous_san_says_how_to_disambiguate():
    # Both rooks reach d1 -- the king is off the rank, so neither is blocked.
    board = chess.Board("4k3/8/8/8/4K3/8/8/R6R w - - 0 1")
    move, reason = read_move(board, "Rd1")
    assert move is None
    assert "ambiguous" in reason


def test_nonsense_says_it_cannot_be_read():
    move, reason = read_move(chess.Board(), "banana")
    assert move is None
    assert "not a move I can read" in reason
