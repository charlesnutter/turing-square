"""Engine strength handling, and the engine as a drop-in move source.

Most of this needs no Stockfish: a fake engine is enough to prove the game core
cannot tell one move source from another, which is the property that matters.
"""

import threading

import chess
import pytest

from chessboard.core.engine import (
    ChessEngine, StockfishEngine, Strength, open_engine,
)
from chessboard.core.game import Game
from chessboard.drivers.led import ConsoleLEDDriver
from chessboard.events import EventBus
from chessboard.session import Session

needs_stockfish = pytest.mark.skipif(
    not StockfishEngine.available(), reason="stockfish not on PATH"
)


# --------------------------------------------------------------------------
# strength -- the two mechanisms are not interchangeable
# --------------------------------------------------------------------------

def test_elo_and_skill_are_mutually_exclusive():
    with pytest.raises(ValueError, match="not both"):
        Strength(elo=1500, skill=5)


def test_elo_below_the_floor_is_rejected_and_points_at_skill():
    """UCI_Elo bottoms out at 1320; asking for 800 must not silently clamp."""
    with pytest.raises(ValueError, match="weaker play use skill"):
        Strength(elo=800)


@pytest.mark.parametrize("bad", [-1, 21])
def test_skill_range_is_enforced(bad):
    with pytest.raises(ValueError, match="Skill Level"):
        Strength(skill=bad)


def test_elo_sets_limitstrength_and_skill_does_not():
    elo = Strength(elo=1500).as_options()
    assert elo["UCI_LimitStrength"] is True and elo["UCI_Elo"] == 1500
    assert "Skill Level" not in elo

    skill = Strength(skill=3).as_options()
    assert skill["Skill Level"] == 3
    assert "UCI_LimitStrength" not in skill and "UCI_Elo" not in skill


def test_full_strength_sets_neither():
    options = Strength().as_options()
    assert "UCI_Elo" not in options and "Skill Level" not in options


# --------------------------------------------------------------------------
# the engine is just another BoardInput
# --------------------------------------------------------------------------

class FakeEngine:
    """Anything with play(board) -> Move works. Nothing imports the real one."""

    def __init__(self):
        self.calls = 0

    def play(self, board):
        self.calls += 1
        return sorted(board.legal_moves, key=lambda m: m.uci())[0]


def quiet_session(game, engines=None):
    return Session(game, ConsoleLEDDriver(echo=lambda _: None),
                   echo=lambda _: None, engines=engines or {}, inputs=[])


def play_until(session, game, ply, typed=()):
    """Run the loop off-thread, feed it lines, and stop once the game gets there.

    The engine answers on its own thread, so the test cannot simply queue
    everything up front and call run() -- it has to watch the game advance.
    """
    bus = EventBus()
    for text in typed:
        bus.post("keyboard", text)
    thread = threading.Thread(target=lambda: session.run(bus), daemon=True)
    thread.start()
    tick = threading.Event()
    for _ in range(400):
        if game.ply >= ply:
            break
        tick.wait(0.005)
    bus.post("keyboard", "quit")
    thread.join(2.0)


def test_the_session_alternates_between_a_keyboard_and_an_engine():
    """The core must not be able to tell a keyboard from an engine."""
    game = Game()
    fake = FakeEngine()
    play_until(quiet_session(game, {chess.BLACK: fake}), game, 2, typed=["e4"])

    assert game.san_history[0] == "e4"
    assert fake.calls == 1           # the engine answered Black's turn
    assert game.ply == 2


def test_no_engine_at_all_is_local_two_player():
    """Both colours typed by people -- the same class, with an empty mapping."""
    game = Game()
    session = quiet_session(game)
    bus = EventBus()
    for text in ("e4", "e5", "quit"):
        bus.post("keyboard", text)
    session.run(bus)
    assert game.san_history == ["e4", "e5"]


# --------------------------------------------------------------------------
# the real binary
# --------------------------------------------------------------------------

@needs_stockfish
def test_engine_plays_a_legal_move_at_low_skill():
    with StockfishEngine(strength=Strength(skill=0)) as engine:
        board = chess.Board()
        assert engine.play(board) in board.legal_moves


@needs_stockfish
def test_engine_refuses_to_move_in_a_finished_game():
    board = chess.Board("7k/5QQ1/8/8/8/8/8/7K b - - 0 1")
    assert board.is_game_over()
    with StockfishEngine(strength=Strength(skill=0)) as engine:
        with pytest.raises(ValueError, match="finished game"):
            engine.play(board)


@needs_stockfish
def test_analyse_returns_multipv_lines_with_scores():
    with StockfishEngine(strength=Strength(skill=5)) as engine:
        info = engine.analyse(chess.Board(), multipv=3)
        assert len(info) == 3
        assert all("score" in line for line in info)


# --------------------------------------------------------------------------
# the engine interface -- so adding Maia adds an engine, not a play mode
# --------------------------------------------------------------------------

class ToyEngine(ChessEngine):
    """A second implementation, to prove nothing is Stockfish-shaped."""

    @property
    def name(self):
        return "toy"

    def play(self, board):
        return sorted(board.legal_moves, key=lambda m: m.uci())[0]


def test_a_non_stockfish_engine_satisfies_the_interface():
    with ToyEngine() as engine:
        board = chess.Board()
        assert engine.play(board) in board.legal_moves
        assert engine.describe() == "toy"
        assert engine.analyse(board) == []      # cannot evaluate, says so


def test_an_engine_without_strength_knobs_still_drives_a_session():
    """Maia has no UCI_Elo or Skill Level -- you pick a net. That must be fine."""
    game = Game()
    play_until(quiet_session(game, {chess.BLACK: ToyEngine()}), game, 2, typed=["e4"])
    assert game.ply == 2


def test_unknown_engine_names_the_known_ones():
    from chessboard.core.engine import EngineUnavailable
    with pytest.raises(EngineUnavailable, match="stockfish"):
        open_engine("maia")


@needs_stockfish
def test_open_engine_returns_a_chess_engine():
    with open_engine("stockfish", strength=Strength(skill=1)) as engine:
        assert isinstance(engine, ChessEngine)
