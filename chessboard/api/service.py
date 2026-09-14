"""The service between HTTP and the game loop.

The loop owns the board and consumes one queue. This turns a request into
something on that queue and hands back what came of it, so the HTTP layer never
touches the game: a second writer is exactly what the single queue exists to
prevent.

A request does not get its answer by reaching in, then. It posts, waits for the
loop to say the state changed, and reports the state it finds. That is also why
a move is checked for legality *before* it is posted -- the loop's refusal goes
to the echo, not back up the socket, and a client that types an illegal move
deserves the reason rather than silence.
"""

import threading
from typing import Callable, List, Optional

import chess
import chess.engine

from ..core.engine import EngineUnavailable, Strength, open_engine as _open_engine
from ..core.game import Game, explain_illegal
from ..drivers.board_input import read_move
from ..drivers.led import ConsoleLEDDriver, LEDDriver
from ..events import EventBus
from ..modes import LOCAL_AI, LOCAL_HUMAN, GameRequest, resolve_mode
from ..session import COMMANDS, Session
from .state import snapshot

# `quit` is how the terminal leaves; over HTTP that is what DELETE /game means,
# and letting it through as a command would end the game from a stray request.
API_COMMANDS = tuple(c for c in COMMANDS if c not in ("quit", "q", "exit"))

SETTLE = 2.0        # how long a request waits for the loop to pick its work up

# Every state a client is ever handed carries this, so a view reads one shape
# whether a game is running, has just ended, or was never started.
IDLE = {"running": False}


class ServiceError(Exception):
    """A refusal with a status, so the route layer does not have to guess one."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class GameService:
    """Owns the running game, its loop and its watchers.

    `open_engine` is injected so a test never shells out to Stockfish, and so
    swapping in Maia later is a wiring change rather than a code change here.
    """

    def __init__(self, open_engine: Callable[..., object] = _open_engine,
                 leds: Optional[LEDDriver] = None,
                 echo: Callable[[str], None] = print):
        self._open_engine = open_engine
        self._leds = leds if leds is not None else ConsoleLEDDriver()
        self._echo = echo
        self._lock = threading.RLock()
        self._watchers: List[Callable[[dict], None]] = []
        self._changed = threading.Event()

        self._session: Optional[Session] = None
        self._bus: Optional[EventBus] = None
        self._thread: Optional[threading.Thread] = None
        self._mode: Optional[str] = None
        self._engine_names: dict = {}

    # ---- watchers ----------------------------------------------------------

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        """Register a watcher and get back the way to remove it."""
        with self._lock:
            self._watchers.append(callback)

        def cancel() -> None:
            with self._lock:
                if callback in self._watchers:
                    self._watchers.remove(callback)

        return cancel

    def _broadcast(self) -> None:
        """Called from the loop thread after every event it handles."""
        try:
            self._notify(self.state())
        except ServiceError:
            return

    def _notify(self, state: dict) -> None:
        self._changed.set()
        for watcher in list(self._watchers):
            try:
                watcher(state)
            except Exception:       # noqa: BLE001 -- a dropped socket is not
                pass                # a reason to stop the game for everyone else

    # ---- state -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._session is not None

    def _require_game(self) -> Session:
        session = self._session
        if session is None:
            raise ServiceError("no game is running -- start one first", 409)
        return session

    def state(self) -> dict:
        session = self._require_game()
        return {"running": True,
                **snapshot(session.game, mode=self._mode,
                           engines=self._engine_names, thinking=session.thinking)}

    # ---- starting and stopping ---------------------------------------------

    def start(self, request: GameRequest) -> dict:
        mode, error = resolve_mode(request)
        if error:
            raise ServiceError(error)
        if mode not in (LOCAL_HUMAN, LOCAL_AI):
            raise ServiceError(
                f"{mode} is not available over the API yet -- it would create a "
                f"real game on the Lichess account. Use the command line.", 501)

        try:
            game = Game(request.fen)
        except ValueError as exc:
            raise ServiceError(f"bad FEN: {exc}") from None

        engines, names = self._build_engines(request, mode)

        # One game at a time -- this is the tablet's New Game. It replaces the
        # running game rather than ending it, so no idle state is announced in
        # between: a client seeing running=false would conclude the game it was
        # watching had finished.
        self.stop(notify=False)
        with self._lock:
            self._mode = mode
            self._engine_names = names
            self._bus = EventBus()
            self._session = Session(game, self._leds, echo=self._echo,
                                    engines=engines, inputs=[])
            self._session.on_change = self._broadcast
            self._thread = threading.Thread(
                target=self._session.run, args=(self._bus,),
                name="game-loop", daemon=True)
            self._thread.start()
        # A view that was already open has no request to learn from: without
        # this it sits on "nothing running" until somebody happens to move.
        state = self.state()
        self._notify(state)
        return state

    def _build_engines(self, request: GameRequest, mode: str):
        if mode != LOCAL_AI:
            return {}, {}
        try:
            strength = Strength(elo=request.elo, skill=request.skill,
                                threads=request.threads)
        except ValueError as exc:
            raise ServiceError(str(exc)) from None

        limit = (chess.engine.Limit(time=request.movetime / 1000.0)
                 if request.movetime else chess.engine.Limit(depth=request.depth))
        try:
            engine = self._open_engine(request.engine, strength=strength, limit=limit)
        except EngineUnavailable as exc:
            raise ServiceError(str(exc), 503) from None

        seat = chess.WHITE if request.black else chess.BLACK
        return {seat: engine}, {seat: getattr(engine, "name", request.engine)}

    def stop(self, notify: bool = True) -> dict:
        """End the running game. Safe to call when nothing is running."""
        with self._lock:
            session, bus, thread = self._session, self._bus, self._thread
            self._session = self._bus = self._thread = None
            self._mode = None
            self._engine_names = {}
        if session is None:
            return dict(IDLE)
        session.on_change = None
        bus.post("api", "quit")
        thread.join(SETTLE)
        bus.stop()
        # Same reason as start: every open view is showing a game that is over.
        if notify:
            self._notify(dict(IDLE))
        return dict(IDLE)

    # ---- submitting --------------------------------------------------------

    def play(self, text: str) -> dict:
        """Check the move against the board we can see, then post it to the loop."""
        session = self._require_game()
        if text.strip().lower() in COMMANDS:
            raise ServiceError(f"{text!r} is a command, not a move")
        if session.game.is_over:
            raise ServiceError("the game is over", 409)
        if session.game.turn in session.engines:
            raise ServiceError("it is the engine's move", 409)

        board = session.game.board
        move, reason = read_move(board, text)
        if move is None:
            raise ServiceError(reason or f"{text!r} is not a move I can read")
        if move not in board.legal_moves:
            raise ServiceError(explain_illegal(board, move))

        # The loop may have moved on between that check and this post -- an
        # engine answering, say. It re-checks legality, and the refusal reaches
        # every watcher as the next pushed state, so nobody is left out of date.
        return self._post(move)

    def command(self, text: str) -> dict:
        self._require_game()
        name = text.strip().lower()
        if name not in API_COMMANDS:
            raise ServiceError(
                f"{text!r} is not a command -- try {', '.join(API_COMMANDS)}")
        return self._post(name)

    def _post(self, payload) -> dict:
        """Hand the loop some work and report the state once it has run."""
        bus = self._bus
        if bus is None:
            raise ServiceError("no game is running -- start one first", 409)
        self._changed.clear()
        bus.post("api", payload)
        self._changed.wait(SETTLE)
        return self.state()
