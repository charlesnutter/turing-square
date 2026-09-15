"""The HTTP surface.

Commands in over REST, state out over a WebSocket. Every route is a thin
translation of a service call into a status code -- nothing here decides
anything about chess, because the Pi's authority lives in the game loop and a
route that knew the rules would be a second opinion.

The push is the important half. A move made on the board, or by an engine that
was never asked over HTTP, has no request to answer: only a subscriber hears
about it. That is what stops the tablet and the board disagreeing.
"""

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..modes import MODES, GameRequest
from .service import IDLE, GameService, ServiceError

WEB = Path(__file__).resolve().parent.parent / "web"


class NewGame(BaseModel):
    """The JSON body of POST /api/game -- the fields of a GameRequest.

    An unknown mode is rejected here rather than deeper in, so a typo comes back
    as a validation error naming the four modes instead of a guess.
    """

    mode: Optional[str] = Field(default=None, pattern="|".join(MODES))
    black: bool = False
    fen: Optional[str] = None
    engine: str = "stockfish"
    elo: Optional[int] = None
    skill: Optional[int] = None
    depth: int = 12
    movetime: Optional[int] = None
    threads: int = 1


class MoveBody(BaseModel):
    move: str


class CommandBody(BaseModel):
    command: str


def create_app(service: Optional[GameService] = None) -> FastAPI:
    """Build the app around a service, so tests can inject one with a toy engine."""
    service = service if service is not None else GameService()
    app = FastAPI(title="chessboard")
    app.state.service = service

    def guard(call):
        try:
            return call()
        except ServiceError as refused:
            raise HTTPException(status_code=refused.status,
                                detail=refused.message) from None

    @app.get("/api/state")
    def read_state() -> dict:
        # Not an error: opening the tablet before starting a game is normal, and
        # a view should not need an error path for its own first request.
        try:
            return service.state()
        except ServiceError:
            return dict(IDLE)

    @app.post("/api/game")
    def start_game(body: NewGame = Body(default_factory=NewGame)) -> dict:
        return guard(lambda: service.start(GameRequest(**body.model_dump())))

    @app.delete("/api/game")
    def stop_game() -> dict:
        service.stop()
        return dict(IDLE)

    @app.post("/api/move")
    def play_move(body: MoveBody) -> dict:
        return guard(lambda: service.play(body.move))

    @app.post("/api/command")
    def run_command(body: CommandBody) -> dict:
        return guard(lambda: service.command(body.command))

    @app.websocket("/ws")
    async def push_state(socket: WebSocket) -> None:
        await socket.accept()
        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[dict]" = asyncio.Queue()

        # The service calls this from the game loop's thread, which has no
        # business touching an asyncio socket -- hand the state over instead.
        def on_state(state: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, state)

        cancel = service.subscribe(on_state)
        try:
            try:
                await socket.send_json(service.state())
            except ServiceError:
                await socket.send_json(dict(IDLE))
            while True:
                await socket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            cancel()

    # Mounted last and at the root, so every /api path and /ws is matched by the
    # routes above before the static files ever get a look at it.
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")

    return app


app = create_app()
