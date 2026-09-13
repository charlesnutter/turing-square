"""Play one Lichess game from the board.

Lichess is the authority on game state here, not us. Every `gameState` carries
the full move list, so the board is **rebuilt from that list** rather than having
our own move applied locally and the echo discarded. That removes any chance of
applying a move twice, and it means a dropped-and-reconnected stream re-syncs for
free.

Rebuilding through `python-chess` also validates what Lichess sent, which has
caught nothing so far and costs nothing.
"""

from typing import Callable, Optional

import chess

from ..core.game import Game
from ..drivers.board_input import BoardInput, Quit
from ..drivers.led import GREEN, LEDDriver
from .client import Client, LichessError

LIVE = "started"


class LichessGame:
    def __init__(self, client: Client, game_id: str, board_input: BoardInput,
                 leds: LEDDriver, username: str,
                 echo: Callable[[str], None] = print):
        self.client = client
        self.game_id = game_id
        self.input = board_input
        self.leds = leds
        self.username = username
        self.echo = echo
        self.my_color: Optional[chess.Color] = None
        self._initial_fen: Optional[str] = None
        self.game = Game()

    # ---- state -------------------------------------------------------------

    def _rebuild(self, moves: str) -> Game:
        game = Game(self._initial_fen)
        for uci in moves.split():
            game.play(chess.Move.from_uci(uci))
        return game

    def _adopt(self, full: dict) -> None:
        """Take colour and starting position from the opening gameFull event."""
        initial = full.get("initialFen", "startpos")
        self._initial_fen = None if initial == "startpos" else initial

        white = (full.get("white") or {}).get("id", "")
        black = (full.get("black") or {}).get("id", "")
        me = self.username.lower()
        if white.lower() == me:
            self.my_color = chess.WHITE
        elif black.lower() == me:
            self.my_color = chess.BLACK
        else:
            raise LichessError(
                f"{self.username} is not a player in {self.game_id} "
                f"(white={white!r}, black={black!r})"
            )
        opponent = black if self.my_color == chess.WHITE else white
        side = "White" if self.my_color == chess.WHITE else "Black"
        self.echo(f"  game {self.game_id}: you are {side} against {opponent or 'AI'}")

    # ---- display -----------------------------------------------------------

    def _show(self) -> None:
        self.echo("")
        self.echo(self.game.board.unicode(borders=False, empty_square="."))
        if self.game.san_history:
            self.echo(f"  last: {self.game.san_history[-1]}")
        if self.game.board.is_check():
            self.echo("  check")

    def _light_last(self) -> None:
        if self.game.board.move_stack:
            last = self.game.board.move_stack[-1]
            self.leds.light_only([last.from_square, last.to_square], GREEN)

    # ---- the turn ----------------------------------------------------------

    def _send_my_move(self) -> bool:
        """Prompt and POST until Lichess accepts one. False if the player quit."""
        while True:
            try:
                move = self.input.next_move(self.game.board)
            except Quit:
                return False
            if move is None:
                continue
            if move not in self.game.board.legal_moves:
                from ..core.game import explain_illegal
                self.echo(f"  no -- {explain_illegal(self.game.board, move)}")
                continue
            try:
                self.client.make_move(self.game_id, move.uci())
                return True
            except LichessError as exc:
                # Rejected upstream: the clock ran out, the game ended, or our
                # board drifted. Say so rather than silently looping.
                self.echo(f"  Lichess rejected {move.uci()}: {exc}")
                return True  # let the next gameState tell us where we really are

    def _sync(self, state: dict) -> Optional[str]:
        """Returns a result string once the game is over, else None."""
        self.game = self._rebuild(state.get("moves", ""))
        self._light_last()
        self._show()

        status = state.get("status", LIVE)
        if status != LIVE:
            winner = state.get("winner")
            if winner:
                mine = (winner == "white") == (self.my_color == chess.WHITE)
                return f"{winner} wins by {status} — {'you won' if mine else 'you lost'}"
            return f"game over: {status}"

        if self.game.board.turn == self.my_color:
            if not self._send_my_move():
                return "aborted"
        else:
            self.echo("  waiting for your opponent...")
        return None

    # ---- loop --------------------------------------------------------------

    def run(self) -> str:
        for event in self.client.stream_game(self.game_id):
            kind = event.get("type")
            if kind == "gameFull":
                self._adopt(event)
                result = self._sync(event.get("state", {}))
            elif kind == "gameState":
                result = self._sync(event)
            elif kind == "chatLine":
                self.echo(f"  [{event.get('username')}] {event.get('text')}")
                continue
            else:
                continue

            if result is not None:
                self.echo(f"\n  {result}")
                self.echo(f"  {self.game.movetext()}")
                return result
        return "stream ended"


def wait_for_game(client: Client, echo: Callable[[str], None] = print) -> Optional[str]:
    """Block on the event stream until a game starts, and return its id."""
    echo("  waiting for a game to start — open a challenge on lichess.org")
    for event in client.stream_events():
        kind = event.get("type")
        if kind == "gameStart":
            return (event.get("game") or {}).get("id")
        if kind == "challenge":
            challenge = event.get("challenge") or {}
            echo(f"  challenge from {(challenge.get('challenger') or {}).get('name', '?')} "
                 f"— accept it on lichess.org")
    return None
