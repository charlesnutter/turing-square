"""Play one Lichess game.

Lichess is the authority on game state. Every `gameState` carries the full move
list, so the board is **rebuilt from that list** rather than having our own move
applied locally and the echo discarded. That removes any chance of applying a
move twice, and a dropped-and-reconnected stream re-syncs for free. Rebuilding
through `python-chess` also validates what Lichess sent.

The stream and the keyboard are both producers on one queue, so neither blocks
the other: a move you play in a browser shows up in the terminal immediately,
even while the prompt is waiting.
"""

import sys
from typing import Callable, Iterable, Optional

import chess

from ..core.game import Game, explain_illegal
from ..drivers.board_input import parse_move
from ..drivers.led import GREEN, LEDDriver
from ..events import EOF, ERROR, EventBus
from .client import Client, LichessError

LIVE = "started"
COMMANDS = ("moves", "fen", "board", "resign", "quit", "q", "exit", "help")


def stdin_lines() -> Iterable[str]:
    for line in sys.stdin:
        yield line.rstrip("\n")


class LichessGame:
    def __init__(self, client: Client, game_id: str, leds: LEDDriver,
                 username: str, lines: Optional[Iterable[str]] = None,
                 echo: Callable[[str], None] = print):
        self.client = client
        self.game_id = game_id
        self.leds = leds
        self.username = username
        self.echo = echo
        self._lines = lines
        self.my_color: Optional[chess.Color] = None
        self.game = Game()
        self._initial_fen: Optional[str] = None
        self._my_turn = False
        self._result: Optional[str] = None

    # ---- state -------------------------------------------------------------

    def _rebuild(self, moves: str) -> Game:
        game = Game(self._initial_fen)
        for uci in moves.split():
            game.play(chess.Move.from_uci(uci))
        return game

    def _adopt(self, full: dict) -> None:
        initial = full.get("initialFen", "startpos")
        self._initial_fen = None if initial == "startpos" else initial

        white = (full.get("white") or {}).get("id", "") or ""
        black = (full.get("black") or {}).get("id", "") or ""
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
        side = "White" if self.my_color == chess.WHITE else "Black"
        opponent = black if self.my_color == chess.WHITE else white
        self.echo(f"  game {self.game_id}: you are {side} against {opponent or 'the AI'}")

    # ---- display -----------------------------------------------------------

    def _show(self) -> None:
        self.echo("")
        self.echo(self.game.board.unicode(borders=False, empty_square="."))
        if self.game.san_history:
            self.echo(f"  last: {self.game.san_history[-1]}")
        if self.game.board.is_check():
            self.echo("  check")

    def _prompt(self) -> None:
        if self._my_turn and self._result is None:
            self.echo("  your move  (moves | fen | board | resign | quit)")

    def _light_last(self) -> None:
        if self.game.board.move_stack:
            last = self.game.board.move_stack[-1]
            self.leds.light_only([last.from_square, last.to_square], GREEN)

    # ---- commands ----------------------------------------------------------

    def handle_command(self, command: str) -> Optional[str]:
        """Returns a result string if the game should end, else None."""
        command = command.lower()
        if command in ("quit", "q", "exit"):
            self.echo("  leaving the game running on lichess.org")
            return "aborted"
        if command == "resign":
            try:
                self.client.resign(self.game_id)
                self.echo("  resigned")
            except LichessError as exc:
                self.echo(f"  could not resign: {exc}")
            return None
        if command == "moves":
            self.echo("  " + " ".join(self.game.legal_san()))
        elif command == "fen":
            self.echo("  " + self.game.fen)
        elif command == "board":
            self._show()
        elif command == "help":
            self.echo("  type a move as SAN (Nf3) or UCI (g1f3), or: "
                      + " ".join(COMMANDS))
        self._prompt()
        return None

    # ---- event handlers ----------------------------------------------------

    def _sync(self, state: dict) -> Optional[str]:
        self.game = self._rebuild(state.get("moves", ""))
        self._light_last()
        self._show()

        status = state.get("status", LIVE)
        if status != LIVE:
            winner = state.get("winner")
            if winner:
                mine = (winner == "white") == (self.my_color == chess.WHITE)
                return (f"{winner} wins by {status} — "
                        f"{'you won' if mine else 'you lost'}")
            return f"game over: {status}"

        self._my_turn = self.game.board.turn == self.my_color
        if self._my_turn:
            self._prompt()
        else:
            self.echo("  waiting for your opponent...")
        return None

    def on_stream(self, event: dict) -> Optional[str]:
        kind = event.get("type")
        if kind == "gameFull":
            self._adopt(event)
            # gameFull embeds a full gameState -- if it is already our move, act
            # on it rather than waiting for a gameState that will never come.
            return self._sync(event.get("state", {}))
        if kind == "gameState":
            return self._sync(event)
        if kind == "chatLine":
            self.echo(f"  [{event.get('username')}] {event.get('text')}")
            self._prompt()
        return None

    def on_line(self, text: str) -> Optional[str]:
        text = text.strip()
        if not text:
            self._prompt()
            return None
        if text.lower() in COMMANDS:
            return self.handle_command(text)

        # Check the turn before parsing. SAN is read relative to the side to
        # move, so typing your own move during the opponent's turn would other-
        # wise fail as "not legal in this position", which blames the wrong thing.
        if not self._my_turn:
            self.echo("  not your move yet — waiting for your opponent")
            return None

        move = parse_move(self.game.board, text, self.echo)
        if move is None:
            self._prompt()
            return None
        if move not in self.game.board.legal_moves:
            self.echo(f"  no -- {explain_illegal(self.game.board, move)}")
            self._prompt()
            return None
        try:
            self.client.make_move(self.game_id, move.uci())
            self._my_turn = False
        except LichessError as exc:
            # Rejected upstream: our board drifted, the clock ran out, or the
            # game ended. The next gameState says where we really are.
            self.echo(f"  Lichess rejected {move.uci()}: {exc}")
        return None

    # ---- loop --------------------------------------------------------------

    def run(self, bus: Optional[EventBus] = None) -> str:
        own_bus = bus is None
        if own_bus:
            bus = EventBus()
            bus.add("lichess", self.client.stream_game(self.game_id))
            bus.add("stdin", self._lines if self._lines is not None else stdin_lines())
        try:
            while True:
                event = bus.get()
                if event is None:
                    continue
                if event.kind == ERROR:
                    if event.source == "lichess":
                        raise event.payload
                    return self._finish("input failed")
                if event.kind == EOF:
                    if event.source == "stdin":
                        return self._finish("aborted")
                    return self._finish("stream ended")

                handler = self.on_stream if event.source == "lichess" else self.on_line
                result = handler(event.payload)
                if result is not None:
                    return self._finish(result)
        finally:
            if own_bus:
                bus.stop()

    def _finish(self, result: str) -> str:
        self._result = result
        self.echo(f"\n  {result}")
        self.echo(f"  {self.game.movetext()}")
        return result


def wait_for_game(client: Client, echo: Callable[[str], None] = print) -> Optional[str]:
    """Block on the event stream until a game starts, and return its id."""
    echo("  waiting for a game to start — open a challenge on lichess.org")
    for event in client.stream_events():
        kind = event.get("type")
        if kind == "gameStart":
            return (event.get("game") or {}).get("id")
        if kind == "challenge":
            name = ((event.get("challenge") or {}).get("challenger") or {}).get("name", "?")
            echo(f"  challenge from {name} — accept it on lichess.org")
    return None
