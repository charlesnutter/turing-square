"""The move input boundary.

An input produces *intent* -- a typed line now, an occupancy frame in Phase 2 --
and the session decides what that intent means against the current board. The
game core never learns whether a move came from a keyboard or from 64 magnets.

It is a producer rather than something the loop calls and waits on. The loop
has to stay free to serve every other source while this one is waiting on a
person, which is why `next_move` is gone: there is no longer any point at which
the loop can be held by a single input.
"""

import re
import sys
from abc import ABC, abstractmethod
from typing import Callable, Iterable, Iterator, Optional, Tuple

import chess

# python-chess's SAN parser also accepts UCI-shaped text (it reads `e1e3` as a
# pawn move to e3 disambiguated from e1), and then raises before a Move object
# exists. Spotting UCI first keeps those moves intact so the core can explain
# exactly why they are illegal.
UCI = re.compile(r"^[a-h][1-8][a-h][1-8][qrbnQRBN]?$")


def read_move(board: chess.Board,
              text: str) -> Tuple[Optional[chess.Move], Optional[str]]:
    """SAN or UCI to a Move, or None and the reason it could not be read.

    The reason is returned rather than printed because not every caller has a
    terminal. Losing it is how an illegal-but-perfectly-spelled `Qd5` came back
    over HTTP as "not a move I can read", which sends a player hunting for a
    typo that is not there.

    UCI is matched first: python-chess's SAN parser also accepts UCI-shaped text
    and then raises before a Move object exists, which would stop an illegal move
    ever reaching the core's explainer -- and the core explains it far better
    than the parser can.
    """
    text = text.strip()
    if not text:
        return None, None
    if UCI.match(text):
        return chess.Move.from_uci(text.lower()), None
    try:
        return board.parse_san(text), None
    except chess.AmbiguousMoveError:
        return None, f"{text} is ambiguous -- name the file or rank, as in Nbd2"
    except chess.IllegalMoveError:
        return None, f"{text} is not legal in this position"
    except chess.InvalidMoveError:
        return None, f"{text!r} is not a move I can read"


def parse_move(board: chess.Board, text: str,
               echo: Callable[[str], None] = lambda _: None) -> Optional[chess.Move]:
    """`read_move` for a caller that would rather print the reason than read it."""
    move, reason = read_move(board, text)
    if reason:
        echo(f"  ?  {reason}")
    return move


class BoardInput(ABC):
    """A source of player intent, attached to the event bus as a producer.

    `name` is the event source the session sees, so it can tell a typed line
    from an occupancy frame without the producer knowing anything about the game.
    """

    name = "input"

    @abstractmethod
    def __iter__(self) -> Iterator:
        """Yield intent until the source ends. Returning ends the session."""

    def close(self) -> None:
        pass


class KeyboardInput(BoardInput):
    """Phase 0. This is the board until Phase 2.

    Yields raw lines. Commands and moves are told apart by the session, which is
    the only thing that knows whose turn it is -- and a line typed on the
    opponent's turn has to be refused rather than parsed.

    It reads the stream and never writes to it. `input()` would be the obvious
    choice, but it writes its prompt from this thread: hold stdout's lock here
    while the main thread finishes, and CPython's finalizer cannot flush, so a
    clean `quit` ends in a fatal error instead of an exit. The session prints
    whose turn it is from the main thread, which is the prompt anyway.
    """

    name = "keyboard"

    def __init__(self, lines: Optional[Iterable[str]] = None):
        self._lines = lines

    def __iter__(self) -> Iterator[str]:
        lines = self._lines if self._lines is not None else sys.stdin
        try:
            for line in lines:
                yield line.rstrip("\n")
        except (EOFError, KeyboardInterrupt):
            return              # the stream closed, or the player pressed ^C
