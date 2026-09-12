"""Wires a Game to whatever drivers it is handed.

Each side has its own move source. Two keyboards is local two-player; keyboard
against an EngineInput is versus-Stockfish; in Phase 2 one of them becomes a
SerialBoardInput reading magnets. This class does not change for any of it.
"""

from typing import Callable, Mapping, Union

import chess

from .core.game import Game, IllegalMove
from .drivers.board_input import BoardInput, Quit
from .drivers.led import GREEN, LEDDriver

Inputs = Union[BoardInput, Mapping[bool, BoardInput]]


class Session:
    def __init__(self, game: Game, inputs: Inputs, leds: LEDDriver,
                 echo: Callable[[str], None] = print):
        self.game = game
        self.inputs: dict = (
            dict(inputs) if isinstance(inputs, Mapping)
            else {chess.WHITE: inputs, chess.BLACK: inputs}
        )
        self.leds = leds
        self.echo = echo

    def input_for(self, color: chess.Color) -> BoardInput:
        return self.inputs[color]

    # ---- display -----------------------------------------------------------

    def show_board(self) -> None:
        self.echo("")
        self.echo(self.game.board.unicode(borders=False, empty_square="."))
        if self.game.board.is_check():
            self.echo(f"  {self.game.turn_name} is in check")

    def handle_command(self, command: str) -> None:
        if command in ("takeback", "undo"):
            # Two plies, so control comes back to the same player.
            undone = [m for m in (self.game.takeback(), self.game.takeback()) if m]
            self.echo(f"  took back {len(undone)} ply" if undone else "  nothing to take back")
            self.show_board()
        elif command == "moves":
            self.echo("  " + " ".join(self.game.legal_san()))
        elif command == "fen":
            self.echo("  " + self.game.fen)
        elif command == "board":
            self.show_board()

    # ---- loop --------------------------------------------------------------

    def run(self) -> str:
        self.show_board()
        while not self.game.is_over:
            try:
                move = self.input_for(self.game.turn).next_move(self.game.board)
            except Quit:
                self.echo("\n  stopped")
                return "aborted"
            if move is None:
                continue
            try:
                played = self.game.play(move)
            except IllegalMove as bad:
                self.echo(f"  no -- {bad.reason}")
                continue

            self.leds.light_only([played.from_square, played.to_square], GREEN)
            number = (self.game.ply + 1) // 2
            self.echo(f"  {number}. {self.game.san_history[-1]}")
            self.show_board()

        outcome = self.game.outcome_text() or "game over"
        self.echo(f"\n  {outcome}")
        self.echo(f"  {self.game.movetext()}")
        return outcome

    def close(self) -> None:
        for board_input in set(self.inputs.values()):
            board_input.close()
