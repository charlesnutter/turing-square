"""The state snapshot the client renders.

The Pi is the single source of truth and the tablet is a pure view, so this is
the whole contract between them: everything needed to draw the board correctly,
and nothing the client would have to compute for itself.

A FEN alone is not enough. It does not say which move to highlight, what the
move was called, whether the game is over and why, or whether it is even your
turn to touch the pieces -- and a view that has to work any of that out is no
longer a view.
"""

from typing import Mapping, Optional

import chess

from ..core.game import Game


def _history(game: Game) -> list:
    """Each ply as the client needs it: what to print, what to highlight, and
    the position to draw if the player scrubs back to it.

    The position is carried rather than derived because deriving it means a
    chess library in the browser, and python-chess owns the rules.
    """
    return [
        {"san": san, "uci": move.uci(), "fen": fen}
        for san, move, fen in zip(game.san_history, game.board.move_stack,
                                  game.fen_history)
    ]


def _last_move(game: Game) -> Optional[dict]:
    """The two squares to highlight, and what to call the move in the list."""
    if not game.board.move_stack:
        return None
    move = game.board.move_stack[-1]
    return {
        "from": chess.square_name(move.from_square),
        "to": chess.square_name(move.to_square),
        "uci": move.uci(),
        "san": game.san_history[-1] if game.san_history else None,
    }


def snapshot(game: Game, *, mode: Optional[str] = None,
             engines: Optional[Mapping[chess.Color, str]] = None,
             thinking: bool = False, takeback: bool = True) -> dict:
    """One JSON-serialisable picture of the game. No chess objects escape here."""
    engines = engines or {}
    return {
        "mode": mode,
        "fen": game.fen,
        "start_fen": game.initial_fen,
        "turn": "white" if game.turn == chess.WHITE else "black",
        "ply": game.ply,
        "check": game.board.is_check(),
        "over": game.is_over,
        "outcome": game.outcome_text(),
        "history": _history(game),
        "movetext": game.movetext(),
        "last_move": _last_move(game),
        # UCI, not SAN: a board validating a drag has two squares and no idea
        # how to disambiguate them, which is exactly what SAN demands.
        "legal_moves": [move.uci() for move in game.legal_moves()],
        "players": {
            "white": engines.get(chess.WHITE),
            "black": engines.get(chess.BLACK),
        },
        # The engine's turn refuses typed moves, so the view has to be able to
        # grey itself out rather than let a player make a move that is dropped.
        "thinking": thinking,
        # What the client may offer. Without this it has to infer availability
        # from the mode string, and gets to find out it was wrong via a 400.
        "can": {
            "takeback": bool(takeback and game.ply and not game.is_over),
        },
    }
