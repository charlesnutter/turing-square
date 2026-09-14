"""History a client can navigate.

The screen has to show the position after move 12 without replaying the game
for itself: a chess library in the browser would be a second rules engine, and
python-chess owns the rules. So the game records the position each move
produced, and the client renders whichever one it is pointed at.
"""

import chess

from chessboard.core.game import Game

# --------------------------------------------------------------------------
# history a client can navigate
# --------------------------------------------------------------------------

def test_the_starting_position_is_remembered():
    """Scrubbing back to ply 0 needs the position the game began from."""
    game = Game()
    game.play_text("e4")
    assert game.initial_fen == chess.STARTING_FEN


def test_a_game_from_a_position_remembers_that_position():
    fen = "4k3/8/8/8/8/8/8/4K2R w K - 0 1"
    game = Game(fen)
    game.play_text("Rh8#")
    assert game.initial_fen == fen


def test_every_move_records_the_position_it_produced():
    """The client renders a past position rather than replaying the game --
    a second rules engine in the browser is exactly what we do not want."""
    game = Game()
    for text in ("e4", "e5", "Nf3"):
        game.play_text(text)

    assert len(game.fen_history) == len(game.san_history) == 3
    after_e4 = chess.Board(game.fen_history[0])
    assert after_e4.piece_at(chess.E4) == chess.Piece(chess.PAWN, chess.WHITE)
    assert chess.Board(game.fen_history[-1]).fen() == game.fen


def test_taking_back_forgets_the_position_too():
    game = Game()
    game.play_text("e4")
    game.play_text("e5")
    game.takeback()
    assert game.san_history == ["e4"]
    assert len(game.fen_history) == 1
    assert chess.Board(game.fen_history[0]).turn == chess.BLACK


def test_history_stays_in_step_through_takebacks_and_replays():
    game = Game()
    for text in ("d4", "d5", "c4"):
        game.play_text(text)
    game.takeback()
    game.takeback()
    game.play_text("Nf6")
    assert game.san_history == ["d4", "Nf6"]
    assert len(game.fen_history) == 2
    assert chess.Board(game.fen_history[1]).fen() == game.fen
