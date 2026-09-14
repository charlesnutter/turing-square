"""The local session as an event loop.

A session used to pull from one source at a time: it called `next_move` and
blocked there. That is exactly the shape that made browser-played moves
invisible online, and with a physical board, a tablet and an engine all
producing at once it would fail the same way again.

So every source is a producer on one queue and the loop never blocks on any of
them. These tests are mostly about that: what the loop does *while* something
else is busy.
"""

import threading

import chess
import pytest

from chessboard.core.game import Game
from chessboard.drivers.led import ConsoleLEDDriver
from chessboard.events import EventBus
from chessboard.session import Session

TIMEOUT = 2.0


def quiet_leds():
    return ConsoleLEDDriver(echo=lambda _: None)


def make(fen=None, engines=None, echo=None):
    game = Game(fen)
    session = Session(game, quiet_leds(), echo=echo or (lambda _: None),
                      engines=engines or {}, inputs=[])
    return game, session


def run_in_thread(session, bus):
    """Run the loop off the main thread so the test can post into it."""
    out = {}

    def go():
        try:
            out["result"] = session.run(bus)
        except BaseException as exc:     # surfaced, not swallowed into a timeout
            out["error"] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, out


def until(predicate, timeout=TIMEOUT):
    """Wait for a condition the loop reaches on its own thread."""
    deadline = threading.Event()
    for _ in range(int(timeout * 200)):
        if predicate():
            return True
        deadline.wait(0.005)
    return False


# --------------------------------------------------------------------------
# lines arriving from any producer
# --------------------------------------------------------------------------

def test_a_typed_move_is_played():
    game, session = make()
    bus = EventBus()
    bus.post("keyboard", "e4")
    bus.post("keyboard", "quit")
    session.run(bus)
    assert game.san_history == ["e4"]


def test_an_illegal_typed_move_is_refused_with_a_reason():
    lines = []
    game, session = make(echo=lines.append)
    bus = EventBus()
    bus.post("keyboard", "e5")
    bus.post("keyboard", "quit")
    session.run(bus)
    assert game.ply == 0
    assert any("e5" in line for line in lines)


def test_a_command_is_not_parsed_as_a_move():
    lines = []
    game, session = make(echo=lines.append)
    bus = EventBus()
    bus.post("keyboard", "fen")
    bus.post("keyboard", "quit")
    session.run(bus)
    assert game.ply == 0
    assert any(chess.STARTING_FEN in line for line in lines)


def test_takeback_returns_control_to_the_same_player():
    game, session = make()
    bus = EventBus()
    for text in ("e4", "e5", "takeback", "quit"):
        bus.post("keyboard", text)
    session.run(bus)
    assert game.ply == 0


def test_quit_ends_the_session():
    _, session = make()
    bus = EventBus()
    bus.post("keyboard", "quit")
    assert session.run(bus) == "aborted"


def test_a_closed_input_ends_the_session():
    """Piped stdin running out must not leave the loop spinning."""
    _, session = make()
    bus = EventBus()
    bus.add("keyboard", iter([]))
    assert session.run(bus) == "aborted"


# --------------------------------------------------------------------------
# moves arriving as moves -- an engine, or later the API and the matcher
# --------------------------------------------------------------------------

class ScriptedEngine:
    """Anything with play(board) -> Move. Nothing here imports a real engine."""

    def __init__(self, ucis=()):
        self._ucis = list(ucis)
        self.calls = 0

    def play(self, board):
        self.calls += 1
        if self._ucis:
            return chess.Move.from_uci(self._ucis.pop(0))
        return sorted(board.legal_moves, key=lambda m: m.uci())[0]


def test_an_engine_is_asked_when_it_is_its_turn_and_its_move_is_played():
    engine = ScriptedEngine(["e7e5"])
    game, session = make(engines={chess.BLACK: engine})
    bus = EventBus()
    thread, _ = run_in_thread(session, bus)
    bus.post("keyboard", "e4")
    assert until(lambda: game.san_history == ["e4", "e5"]), game.san_history
    bus.post("keyboard", "quit")
    thread.join(TIMEOUT)
    assert engine.calls == 1


def test_a_move_posted_by_another_producer_is_played():
    """The API's path: a Move object on the bus, from no particular driver."""
    game, session = make()
    bus = EventBus()
    bus.post("api", chess.Move.from_uci("d2d4"))
    bus.post("api", "quit")
    session.run(bus)
    assert game.san_history == ["d4"]


def test_the_loop_answers_a_command_while_the_engine_is_still_thinking():
    """The whole reason for the convergence: thinking must not block the loop."""
    asked = threading.Event()
    release = threading.Event()

    class SlowEngine:
        def play(self, board):
            asked.set()
            release.wait(TIMEOUT)
            return chess.Move.from_uci("e7e5")

    lines = []
    game, session = make(engines={chess.BLACK: SlowEngine()}, echo=lines.append)
    bus = EventBus()
    thread, _ = run_in_thread(session, bus)

    bus.post("keyboard", "e4")
    assert asked.wait(TIMEOUT), "the engine was never asked"

    bus.post("keyboard", "fen")
    # "b" = Black to move: the FEN of the position after 1.e4, answered while
    # the engine is still blocked. (python-chess omits the en passant square
    # unless an en passant capture is actually legal, so do not look for it.)
    answered = until(lambda: any("PPPP1PPP/RNBQKBNR b" in line for line in lines))
    assert answered, "the loop was blocked on the engine"
    assert game.ply == 1, "the engine's move landed before it was released"

    release.set()
    assert until(lambda: game.ply == 2)
    bus.post("keyboard", "quit")
    thread.join(TIMEOUT)


def test_an_engine_move_for_a_position_that_has_changed_is_discarded():
    """Take back while it thinks and its answer is for a board that is gone."""
    game, session = make()
    session.engines = {chess.BLACK: ScriptedEngine()}
    bus = EventBus()
    game.play_text("e4")
    stale = chess.Move.from_uci("e7e5")
    session._thinking_ply = 99          # it was asked at a ply we have left
    session.on_move(stale, source="engine")
    assert game.ply == 1


FOOLS_MATE_IN_ONE = "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 3"


def test_a_mating_move_ends_the_loop_without_a_quit():
    """Fool's mate. The loop must finish on its own, not wait for more input."""
    game, session = make(FOOLS_MATE_IN_ONE)
    bus = EventBus()
    bus.post("keyboard", "Qh4#")
    result = session.run(bus)
    assert game.board.is_checkmate()
    assert "wins" in result


def test_an_engine_is_not_asked_once_the_game_is_over():
    """Nothing must be left thinking about a finished position."""
    engine = ScriptedEngine()
    game, session = make(FOOLS_MATE_IN_ONE, engines={chess.WHITE: engine})
    bus = EventBus()
    bus.post("keyboard", "Qh4#")
    session.run(bus)
    assert engine.calls == 0


def test_a_human_cannot_type_a_move_on_the_engine_s_turn():
    """SAN is read relative to the side to move, so this must be refused up
    front -- otherwise typing your own next move plays it for the engine."""
    lines = []
    game, session = make(engines={chess.BLACK: ScriptedEngine()}, echo=lines.append)
    game.play_text("e4")                 # black to move, and black is the engine
    session.on_line("e5")
    assert game.ply == 1
    assert any("not your move" in line for line in lines)


def test_both_sides_can_be_engines():
    """Engine versus engine needs no keyboard at all."""
    game, session = make(engines={chess.WHITE: ScriptedEngine(["e2e4"]),
                                 chess.BLACK: ScriptedEngine(["e7e5"])})
    bus = EventBus()
    thread, _ = run_in_thread(session, bus)
    # They keep going once the script runs out, so catch the opening, not a ply.
    assert until(lambda: game.san_history[:2] == ["e4", "e5"]), game.san_history
    bus.post("keyboard", "quit")
    thread.join(TIMEOUT)
