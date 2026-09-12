"""Local two-player mode -- the first thing that runs end to end.

Wires a Game to whatever BoardInput and LEDDriver it is handed. In Phase 0 that
is the keyboard and the console; in Phase 2 the same class runs unchanged
against magnets and pixels.
"""

from typing import Callable

import chess

from .core.game import Game, IllegalMove
from .drivers.board_input import BoardInput, Quit
from .drivers.led import GREEN, LEDDriver


class Session:
    def __init__(self, game: Game, board_input: BoardInput, leds: LEDDriver,
                 echo: Callable[[str], None] = print):
        self.game = game
        self.input = board_input
        self.leds = leds
        self.echo = echo

    # ---- display -----------------------------------------------------------

    def show_board(self) -> None:
        self.echo("")
        self.echo(self.game.board.unicode(borders=False, empty_square="."))
        if self.game.board.is_check():
            self.echo(f"  {self.game.turn_name} is in check")

    def handle_command(self, command: str) -> None:
        if command in ("takeback", "undo"):
            move = self.game.takeback()
            self.echo(f"  took back {move}" if move else "  nothing to take back")
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
                move = self.input.next_move(self.game.board)
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
            self.echo(f"  {self.game.ply // 2 + self.game.ply % 2}. {self.game.san_history[-1]}")
            self.show_board()

        outcome = self.game.outcome_text() or "game over"
        self.echo(f"\n  {outcome}")
        self.echo(f"  {self.game.movetext()}")
        return outcome


def main() -> None:
    from .drivers.board_input import KeyboardInput
    from .drivers.led import ConsoleLEDDriver

    game = Game()
    session = Session(game, KeyboardInput(), ConsoleLEDDriver())
    session.input._on_command = session.handle_command  # type: ignore[attr-defined]
    print("Local two-player. SAN or UCI; 'moves', 'takeback', 'fen', 'quit'.")
    session.run()
