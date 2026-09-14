"""One local game, driven by events.

This used to pull: it called `next_move` on whichever source owned the turn and
blocked there until that source answered. That is the same shape that made
browser-played moves invisible online -- while the terminal waited for a typed
line, nothing was reading the other source -- and with a physical board, a
tablet and an engine all producing at once it would fail the same way again.

So every source is a producer on one queue and the loop never blocks on any of
them. A typed line, an engine's answer, a command from the API and, in Phase 2,
an occupancy frame from 64 magnets all arrive the same way; the loop owns the
board and decides what each one means.

An engine is the one source that cannot be a producer: it has nothing to say
until it is asked. So it is asked on its own thread and posts its answer back,
which keeps the loop free while it thinks.
"""

import threading
from typing import Callable, Iterable, Mapping, Optional

import chess

from .core.game import Game, IllegalMove
from .drivers.board_input import BoardInput, KeyboardInput, parse_move
from .drivers.led import GREEN, LEDDriver
from .events import EOF, ERROR, EventBus

ENGINE = "engine"
COMMANDS = ("moves", "fen", "board", "takeback", "undo", "help",
            "quit", "q", "exit")


class Session:
    """A local game and the loop that feeds it.

    `engines` maps a colour to anything with `play(board) -> Move`; a colour
    with no entry is played by whoever is typing. Two empty entries is local
    two-player, one is versus-engine, two is engine versus engine -- the same
    class for all three, which is the point.
    """

    def __init__(self, game: Game, leds: LEDDriver,
                 echo: Callable[[str], None] = print,
                 engines: Optional[Mapping[chess.Color, object]] = None,
                 inputs: Optional[Iterable[BoardInput]] = None):
        self.game = game
        self.leds = leds
        self.echo = echo
        self.engines = dict(engines or {})
        # None means "attach stdin when you run"; an explicit list, even an
        # empty one, means the caller is supplying the producers itself.
        self.inputs = None if inputs is None else list(inputs)
        # Called after every event the loop handles, including one that changed
        # nothing: a client may have drawn an optimistic board off a move that
        # was then refused, and silence would leave it wrong.
        self.on_change: Optional[Callable[[], None]] = None
        self._thinking_ply: Optional[int] = None
        self._result: Optional[str] = None

    @property
    def thinking(self) -> bool:
        """Whether an engine has been asked and has not answered yet."""
        return self._thinking_ply is not None

    # ---- display -----------------------------------------------------------

    def show_board(self) -> None:
        self.echo("")
        self.echo(self.game.board.unicode(borders=False, empty_square="."))
        if self.game.board.is_check():
            self.echo(f"  {self.game.turn_name} is in check")

    def _prompt(self) -> None:
        if self._result is None and not self.game.is_over \
                and self.game.turn not in self.engines:
            self.echo(f"  {self.game.turn_name} to move")

    def _light(self, move: chess.Move) -> None:
        self.leds.light_only([move.from_square, move.to_square], GREEN)

    # ---- commands ----------------------------------------------------------

    def handle_command(self, command: str) -> Optional[str]:
        """Returns a result string if the session should end, else None."""
        command = command.lower()
        if command in ("quit", "q", "exit"):
            return "aborted"
        if command in ("takeback", "undo"):
            # Go back far enough that it is a person's move again. With an
            # engine in the other seat that is two plies -- one would hand the
            # turn straight back to it and it would simply move again. With two
            # people it is one, because the player who just moved wants their
            # own move back, not their opponent's as well.
            undone = 0
            while self.game.ply and (undone == 0 or self.game.turn in self.engines):
                if self.game.takeback() is None:
                    break
                undone += 1
            self.echo(f"  took back {undone} ply" if undone
                      else "  nothing to take back")
            # Whatever was being thought about is for a board that is now gone.
            self._thinking_ply = None
            self.show_board()
        elif command == "moves":
            self.echo("  " + " ".join(self.game.legal_san()))
        elif command == "fen":
            self.echo("  " + self.game.fen)
        elif command == "board":
            self.show_board()
        elif command == "help":
            self.echo("  a move as SAN (Nf3) or UCI (g1f3), or: "
                      + " ".join(COMMANDS))
        self._prompt()
        return None

    # ---- event handlers ----------------------------------------------------

    def on_line(self, text: str) -> Optional[str]:
        """A line of text from any producer -- a keyboard, or the API."""
        text = text.strip()
        if not text:
            return None
        if text.lower() in COMMANDS:
            return self.handle_command(text)

        # Check whose turn it is before parsing. SAN is read relative to the
        # side to move, so a move typed on the engine's turn would otherwise be
        # parsed as a move *for the engine* and played on its behalf.
        if self.game.turn in self.engines:
            self.echo("  not your move yet — the engine is thinking")
            return None

        move = parse_move(self.game.board, text, self.echo)
        if move is None:
            self._prompt()
            return None
        return self.on_move(move)

    def on_move(self, move: chess.Move, source: Optional[str] = None) -> Optional[str]:
        """A move from anywhere: typed, played by an engine, or posted by the API."""
        if source == ENGINE:
            asked_at, self._thinking_ply = self._thinking_ply, None
            if asked_at != self.game.ply:
                # It answered about a board we have already left, via a takeback
                # or a move from another producer. Its answer is meaningless now.
                return None

        try:
            self.game.play(move)
        except IllegalMove as bad:
            self.echo(f"  no -- {bad.reason}")
            self._prompt()
            return None

        self._light(move)
        number = (self.game.ply + 1) // 2
        self.echo(f"  {number}. {self.game.san_history[-1]}")
        self.show_board()

        if self.game.is_over:
            return self.game.outcome_text() or "game over"
        self._prompt()
        return None

    # ---- the engine --------------------------------------------------------

    def _ask_engine(self, bus: EventBus) -> None:
        """Ask whoever owns this turn, on its own thread.

        Stockfish at depth 12 is seconds of silence. Doing this inline would put
        the blocking call back in the loop, which is the whole thing being fixed.
        """
        if self._thinking_ply is not None or self.game.is_over:
            return
        engine = self.engines.get(self.game.turn)
        if engine is None:
            return

        self._thinking_ply = self.game.ply
        board = self.game.board.copy()      # it must not see our board change
        name = getattr(engine, "name", "engine")
        self.echo(f"  {name} is thinking...")

        def think() -> None:
            try:
                bus.post(ENGINE, engine.play(board))
            except Exception as exc:        # noqa: BLE001 -- reported, not swallowed
                bus.post(ENGINE, exc, ERROR)

        threading.Thread(target=think, name="engine", daemon=True).start()

    # ---- loop --------------------------------------------------------------

    def run(self, bus: Optional[EventBus] = None) -> str:
        own_bus = bus is None
        if own_bus:
            bus = EventBus()
            sources = self.inputs if self.inputs is not None else [KeyboardInput()]
            for source in sources:
                bus.add(source.name, source)

        self.show_board()
        self._prompt()
        try:
            self._ask_engine(bus)
            while True:
                event = bus.get()
                if event is None:
                    continue
                if event.kind == ERROR:
                    self.echo(f"  {event.source} failed: {event.payload}")
                    return self._finish("aborted")
                if event.kind == EOF:
                    return self._finish("aborted")

                payload = event.payload
                if isinstance(payload, chess.Move):
                    result = self.on_move(payload, source=event.source)
                else:
                    result = self.on_line(str(payload))

                if result is not None:
                    return self._finish(result)
                self._ask_engine(bus)
                self._changed()
        finally:
            if own_bus:
                bus.stop()
            self.close()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _finish(self, result: str) -> str:
        self._result = result
        self.echo(f"\n  {result}")
        if self.game.san_history:
            self.echo(f"  {self.game.movetext()}")
        self._changed()
        return result

    def close(self) -> None:
        for source in (self.inputs or []):
            source.close()
        for engine in self.engines.values():
            closer = getattr(engine, "close", None)
            if closer:
                closer()
