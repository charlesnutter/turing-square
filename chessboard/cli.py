"""Command line entry: choose a mode, wire the drivers, run it.

Four modes, two implementations. `local-human` and `local-ai` differ only in
which producer supplies the opponent's moves; `online-human` and `online-ai`
differ only in how the game gets created. The UI count should not drive the
code count, and the same four map onto the buttons the tablet will show.
"""

import argparse
import sys
from typing import Optional, Tuple

import chess
import chess.engine

from .core.engine import (
    ENGINES, EngineUnavailable, Strength, open_engine,
)
from .core.game import Game
from .drivers.led import ConsoleLEDDriver
from .lichess.client import Client, LichessError, load_token
from .lichess.play import LichessGame, wait_for_game
from .modes import (
    LOCAL_AI, LOCAL_HUMAN, MODES, ONLINE_AI, ONLINE_HUMAN, GameRequest,
    resolve_mode,
)
from .session import Session

MODE_HELP = {
    LOCAL_HUMAN: "two players sharing one board",
    LOCAL_AI: "play a local engine — works with the network unplugged",
    ONLINE_HUMAN: "play a person on lichess.org",
    ONLINE_AI: "play Lichess's own engine",
}


def _ai_level(text: str) -> int:
    """Reject an out-of-range level at parse time, before any request is made."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 1 <= value <= 8:
        raise argparse.ArgumentTypeError("Lichess AI level is 1-8")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m chessboard",
        description="Play from the keyboard. No hardware required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "modes:\n"
            + "".join(f"  {m:<14}{MODE_HELP[m]}\n" for m in MODES)
            + "\nexamples:\n"
            "  python -m chessboard                          two players\n"
            "  python -m chessboard --mode local-ai --elo 1500\n"
            "  python -m chessboard --mode local-ai --skill 2 --black\n"
            "  python -m chessboard --mode online-ai --ai-level 1\n"
            "  python -m chessboard --mode online-human\n"
        ),
    )
    p.add_argument("--mode", choices=MODES,
                   help=f"what kind of game (default {LOCAL_HUMAN}, or inferred "
                        f"from the options below)")
    p.add_argument("--black", action="store_true",
                   help="play Black; your opponent moves first")
    p.add_argument("--fen", metavar="FEN", help="start from a position")

    local = p.add_argument_group("local engine (--mode local-ai)")
    local.add_argument("--engine", choices=sorted(ENGINES), default="stockfish",
                       help="which local engine (default stockfish)")
    strength = local.add_mutually_exclusive_group()
    strength.add_argument("--elo", type=int, metavar="N",
                          help=f"strength, {Strength.ELO_MIN}-{Strength.ELO_MAX}")
    strength.add_argument("--skill", type=int, metavar="N",
                          help=f"skill level, {Strength.SKILL_MIN}-{Strength.SKILL_MAX} "
                               f"-- the only way below {Strength.ELO_MIN} Elo")
    local.add_argument("--depth", type=int, default=12, metavar="N",
                       help="search depth (default 12)")
    local.add_argument("--movetime", type=int, metavar="MS",
                       help="milliseconds per move, instead of a depth limit")
    local.add_argument("--threads", type=int, default=1, metavar="N",
                       help="engine threads (default 1)")

    online = p.add_argument_group("lichess (--mode online-*, needs a board:play token)")
    online.add_argument("--ai-level", type=_ai_level, metavar="LEVEL",
                        help="Lichess AI level, 1-8")
    online.add_argument("--game", metavar="ID", dest="game_id",
                        help="join a specific game already in progress")
    online.add_argument("--color", choices=("white", "black", "random"),
                        default="white", help="your colour when challenging the AI")
    online.add_argument("--token-file", metavar="PATH",
                        help="where to read the token (default ~/.lichess-token)")

    short = p.add_argument_group("shorthand")
    short.add_argument("--lichess", action="store_true",
                       help=f"same as --mode {ONLINE_HUMAN}")
    short.add_argument("--lichess-ai", type=_ai_level, metavar="LEVEL",
                       help=f"same as --mode {ONLINE_AI} --ai-level LEVEL")
    return p


def request_from_args(args) -> GameRequest:
    """Argparse's Namespace into the shared request the resolver understands."""
    return GameRequest(
        mode=args.mode, black=args.black, fen=args.fen,
        engine=args.engine, elo=args.elo, skill=args.skill,
        depth=args.depth, movetime=args.movetime, threads=args.threads,
        ai_level=args.ai_level, game_id=args.game_id, color=args.color,
        token_file=args.token_file,
        lichess=args.lichess, lichess_ai=args.lichess_ai,
    )


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.lichess_ai is not None and args.ai_level is None:
        args.ai_level = args.lichess_ai

    request = request_from_args(args)
    mode, error = resolve_mode(request)
    if error:
        print(error, file=sys.stderr)
        return 2

    try:
        game = Game(request.fen)
    except ValueError as exc:
        print(f"bad FEN: {exc}", file=sys.stderr)
        return 2

    leds = ConsoleLEDDriver()
    if mode == LOCAL_HUMAN:
        return _local_human(game, leds)
    if mode == LOCAL_AI:
        return _local_ai(request, game, leds)
    return _online(request, leds, vs_ai=(mode == ONLINE_AI))


# ---- local ----------------------------------------------------------------

def _local_human(game: Game, leds: ConsoleLEDDriver) -> int:
    print("Two players, one keyboard. SAN or UCI; "
          "'moves', 'takeback', 'fen', 'quit'.")
    Session(game, leds).run()
    return 0


def _local_ai(request: GameRequest, game: Game, leds: ConsoleLEDDriver) -> int:
    try:
        strength = Strength(elo=request.elo, skill=request.skill, threads=request.threads)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    limit = (chess.engine.Limit(time=request.movetime / 1000.0) if request.movetime
             else chess.engine.Limit(depth=request.depth))
    try:
        engine = open_engine(request.engine, strength=strength, limit=limit)
    except EngineUnavailable as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    you = chess.BLACK if request.black else chess.WHITE
    print(f"You are {'Black' if request.black else 'White'} against "
          f"{engine.describe()}, {_limit_text(limit)}.")
    print("SAN or UCI; 'moves', 'takeback', 'fen', 'quit'.")
    Session(game, leds, engines={not you: engine}).run()
    return 0


# ---- online ---------------------------------------------------------------

def _online(request: GameRequest, leds: ConsoleLEDDriver, vs_ai: bool) -> int:
    try:
        client = Client(load_token(request.token_file))
        me = client.username()
    except LichessError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"Signed in as {me}.")

    try:
        if request.game_id:
            game_id = request.game_id
        elif vs_ai:
            level = request.ai_level if request.ai_level is not None else 1
            created = client.challenge_ai(level, color=request.color)
            game_id = created.get("id")
            print(f"  challenged the Lichess AI at level {level}")
        else:
            game_id = wait_for_game(client)
    except (LichessError, ValueError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    if not game_id:
        print("no game to play", file=sys.stderr)
        return 1

    print(f"  watch at https://lichess.org/{game_id}")
    print("SAN or UCI; 'moves', 'fen', 'board', 'resign', 'quit'.")
    print("Moves you play in a browser appear here too — no takeback online.")
    try:
        LichessGame(client, game_id, leds, me).run()
    except LichessError as exc:
        print(f"\nlost the game stream: {exc}", file=sys.stderr)
        return 1
    return 0


def _limit_text(limit: chess.engine.Limit) -> str:
    if limit.time:
        return f"{int(limit.time * 1000)}ms/move"
    return f"depth {limit.depth}"
