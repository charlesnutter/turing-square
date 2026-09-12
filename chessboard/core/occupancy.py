"""Occupancy is one bit per square -- all the sensors can ever tell us.

python-chess already keeps exactly this as `board.occupied`, an int bitboard
using the same square numbering (a1 = 0 ... h8 = 63), so the physical board and
the engine speak the same language with no conversion layer.
"""

from typing import Iterable, Iterator

import chess

FULL = (1 << 64) - 1


def occupancy_after(board: chess.Board, move: chess.Move) -> int:
    """The occupancy pattern the board would show once `move` is complete."""
    board.push(move)
    try:
        return board.occupied
    finally:
        board.pop()


def footprint(board: chess.Board, move: chess.Move) -> int:
    """Every square `move` touches *physically*, while it is being played.

    This is deliberately wider than the before/after difference. A capture does
    not change its destination square -- it is occupied before and after -- but
    the captured piece is lifted off it and the capturing piece put down on it,
    so the sensor there goes low and high again in between. A matcher built on
    the difference alone calls that a broken board.

    So: the difference (which covers the origin, a castling rook, and the pawn
    taken en passant), plus the destination square.
    """
    changed = board.occupied ^ occupancy_after(board, move)
    return changed | chess.BB_SQUARES[move.to_square]


def squares(mask: int) -> Iterator[int]:
    return chess.scan_forward(mask)


def names(mask: int) -> list[str]:
    return [chess.square_name(sq) for sq in squares(mask)]


def from_squares(squares_: Iterable[int]) -> int:
    mask = 0
    for square in squares_:
        mask |= chess.BB_SQUARES[square]
    return mask


def render(mask: int) -> str:
    """Eight lines of `#` and `.`, rank 8 first -- for tests and debugging."""
    rows = []
    for rank in range(7, -1, -1):
        rows.append(" ".join(
            "#" if mask & chess.BB_SQUARES[chess.square(f, rank)] else "."
            for f in range(8)
        ))
    return "\n".join(rows)
