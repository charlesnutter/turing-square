"""The game core.

Holds all rules, state and history on python-chess. Stockfish is never asked
whether a move is legal -- only how good it is. Nothing in here knows that
sensors or pixels exist.
"""

import io
from typing import Optional

import chess
import chess.pgn


class IllegalMove(ValueError):
    """Carries a reason fit to show a player, not just a stack trace."""

    def __init__(self, move: chess.Move, reason: str):
        super().__init__(reason)
        self.move = move
        self.reason = reason


def explain_illegal(board: chess.Board, move: chess.Move) -> str:
    """Why this move is not allowed, in words worth putting on a screen."""
    src = chess.square_name(move.from_square)
    dst = chess.square_name(move.to_square)
    piece = board.piece_at(move.from_square)
    mover = "White" if board.turn == chess.WHITE else "Black"

    if piece is None:
        return f"there is no piece on {src}"
    if piece.color != board.turn:
        owner = "White" if piece.color == chess.WHITE else "Black"
        return f"the {chess.piece_name(piece.piece_type)} on {src} is {owner}'s, and it is {mover} to move"
    if move in board.legal_moves:
        return ""

    if piece.piece_type == chess.PAWN and not move.promotion:
        last_rank = 7 if piece.color == chess.WHITE else 0
        if chess.square_rank(move.to_square) == last_rank:
            return f"a pawn reaching {dst} must promote -- say which piece"

    target = board.piece_at(move.to_square)
    if target is not None and target.color == piece.color:
        return f"your own {chess.piece_name(target.piece_type)} is on {dst}"

    if move in board.pseudo_legal_moves:
        checked = "still in check" if board.is_check() else "in check"
        return f"{src}{dst} would leave your king {checked}"

    if piece.piece_type in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        blockers = chess.between(move.from_square, move.to_square) & board.occupied
        if blockers:
            first = chess.square_name(next(chess.scan_forward(blockers)))
            return f"the path from {src} to {dst} is blocked at {first}"

    if piece.piece_type == chess.PAWN and target is not None:
        if chess.square_file(move.from_square) == chess.square_file(move.to_square):
            return f"a pawn cannot capture straight ahead, and {dst} is occupied"

    if board.is_check():
        return f"you are in check, and {src}{dst} does not address it"

    return f"a {chess.piece_name(piece.piece_type)} cannot move from {src} to {dst}"


class Game:
    """One game. The single source of truth for position, legality and history."""

    def __init__(self, fen: Optional[str] = None):
        self.board = chess.Board(fen) if fen else chess.Board()
        self._initial_fen = self.board.fen()
        self._san: list[str] = []

    # ---- state -------------------------------------------------------------

    @property
    def fen(self) -> str:
        return self.board.fen()

    @property
    def turn(self) -> chess.Color:
        return self.board.turn

    @property
    def turn_name(self) -> str:
        return "White" if self.board.turn == chess.WHITE else "Black"

    @property
    def is_over(self) -> bool:
        return self.board.is_game_over()

    @property
    def ply(self) -> int:
        return len(self.board.move_stack)

    def outcome_text(self) -> Optional[str]:
        outcome = self.board.outcome()
        if outcome is None:
            return None
        reason = outcome.termination.name.replace("_", " ").lower()
        if outcome.winner is None:
            return f"draw by {reason}"
        return f"{'White' if outcome.winner == chess.WHITE else 'Black'} wins by {reason}"

    # ---- moves -------------------------------------------------------------

    def legal_moves(self) -> list[chess.Move]:
        return list(self.board.legal_moves)

    def legal_san(self) -> list[str]:
        return sorted(self.board.san(m) for m in self.board.legal_moves)

    def play(self, move: chess.Move) -> chess.Move:
        """Apply a move, or raise IllegalMove with a reason a player can act on."""
        if move not in self.board.legal_moves:
            raise IllegalMove(move, explain_illegal(self.board, move))
        self._san.append(self.board.san(move))
        self.board.push(move)
        return move

    def play_text(self, text: str) -> chess.Move:
        """Accept either SAN (`Nf3`, `exd5`, `O-O`) or UCI (`g1f3`, `e7e8q`)."""
        text = text.strip()
        try:
            return self.play(self.board.parse_san(text))
        except IllegalMove:
            raise
        except ValueError:
            pass
        try:
            move = chess.Move.from_uci(text.lower())
        except ValueError:
            raise IllegalMove(chess.Move.null(), f"{text!r} is not a move I can read")
        return self.play(move)

    def takeback(self) -> Optional[chess.Move]:
        if not self.board.move_stack:
            return None
        self._san.pop()
        return self.board.pop()

    # ---- history -----------------------------------------------------------

    @property
    def san_history(self) -> list[str]:
        return list(self._san)

    def movetext(self) -> str:
        out = []
        for i, san in enumerate(self._san):
            if i % 2 == 0:
                out.append(f"{i // 2 + 1}.")
            out.append(san)
        return " ".join(out)

    def pgn(self, **headers: str) -> str:
        game = chess.pgn.Game.from_board(self.board)
        if self._initial_fen != chess.STARTING_FEN:
            game.headers["FEN"] = self._initial_fen
            game.headers["SetUp"] = "1"
        for key, value in headers.items():
            game.headers[key] = value
        return game.accept(chess.pgn.StringExporter(headers=True, variations=False, comments=False))
