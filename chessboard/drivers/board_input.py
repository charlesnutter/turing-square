"""The move input boundary.

Both implementations answer one question -- "what move does the player want?" --
so the game core never learns whether that came from a keyboard or from 64
magnets. Phase 2 adds SerialBoardInput, which runs the same MoveMatcher over
occupancy frames arriving on the serial port, and changes one line of
wiring-up code.
"""

import re
from abc import ABC, abstractmethod
from typing import Callable, Optional

import chess

# python-chess's SAN parser also accepts UCI-shaped text (it reads `e1e3` as a
# pawn move to e3 disambiguated from e1), and then raises before a Move object
# exists. Spotting UCI first keeps those moves intact so the core can explain
# exactly why they are illegal.
UCI = re.compile(r"^[a-h][1-8][a-h][1-8][qrbnQRBN]?$")


class Quit(Exception):
    """The player asked to stop."""


class BoardInput(ABC):
    @abstractmethod
    def next_move(self, board: chess.Board) -> Optional[chess.Move]:
        """The next move, or None if there is nothing yet. Raise Quit to stop."""

    def close(self) -> None:
        pass


class KeyboardInput(BoardInput):
    """Phase 0. This is the board until Phase 2.

    Accepts SAN or UCI; `takeback`, `moves` and `quit` are handled by the caller
    via the `command` hook so this class stays a pure move source.
    """

    COMMANDS = {"quit", "q", "exit", "takeback", "undo", "moves", "fen", "board"}

    def __init__(self, readline: Callable[[str], str] = input,
                 on_command: Optional[Callable[[str], None]] = None,
                 echo: Callable[[str], None] = print):
        self._readline = readline
        self._on_command = on_command
        self._echo = echo

    def next_move(self, board: chess.Board) -> Optional[chess.Move]:
        side = "White" if board.turn == chess.WHITE else "Black"
        try:
            text = self._readline(f"{side} to move > ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Quit from None

        if not text:
            return None
        if text.lower() in self.COMMANDS:
            if text.lower() in ("quit", "q", "exit"):
                raise Quit
            if self._on_command:
                self._on_command(text.lower())
            return None

        if UCI.match(text):
            # Straight through to the core, legal or not.
            return chess.Move.from_uci(text.lower())

        try:
            return board.parse_san(text)
        except chess.AmbiguousMoveError:
            self._echo(f"  ?  {text} is ambiguous -- name the file or rank, as in Nbd2")
            return None
        except chess.IllegalMoveError:
            # Readable, but not allowed here. SAN cannot be turned into a move
            # object unless it is legal, so this is as far as we can take it.
            self._echo(f"  ?  {text} is not legal in this position")
            return None
        except chess.InvalidMoveError:
            pass

        # UCI reaches the core even when illegal, so the core can say why.
        try:
            return chess.Move.from_uci(text.lower())
        except ValueError:
            self._echo(f"  ?  {text!r} is not a move I can read")
            return None


class EngineInput(BoardInput):
    """An engine as a move source.

    The game core cannot tell this from a keyboard, which is the whole point --
    it is the same swap Phase 2 makes when 64 magnets replace typing. Anything
    with a `play(board) -> Move` method works here; nothing imports the engine.
    """

    def __init__(self, engine, announce: Optional[Callable[[str], None]] = None,
                 name: str = "engine"):
        self._engine = engine
        self._announce = announce
        self._name = name

    def next_move(self, board: chess.Board) -> Optional[chess.Move]:
        if self._announce:
            self._announce(f"  {self._name} is thinking...")
        try:
            return self._engine.play(board)
        except KeyboardInterrupt:
            raise Quit from None

    def close(self) -> None:
        closer = getattr(self._engine, "close", None)
        if closer:
            closer()
