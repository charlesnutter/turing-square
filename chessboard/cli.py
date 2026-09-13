"""Command line entry: pick a mode, wire the drivers, run the session."""

import argparse
import sys
from typing import Optional

import chess
import chess.engine

from .core.engine import Engine, EngineUnavailable, Strength
from .core.game import Game
from .drivers.board_input import EngineInput, KeyboardInput
from .drivers.led import ConsoleLEDDriver
from .lichess.client import Client, LichessError, load_token
from .lichess.play import LichessGame, wait_for_game
from .session import Session


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
            "examples:\n"
            "  python -m chessboard                    two players, one keyboard\n"
            "  python -m chessboard --engine           versus Stockfish, you are White\n"
            "  python -m chessboard --elo 1500         versus Stockfish at ~1500\n"
            "  python -m chessboard --skill 2 --black  a gentle opponent, you are Black\n"
        ),
    )
    p.add_argument("--engine", action="store_true",
                   help="play against Stockfish (implied by --elo, --skill or --black)")
    p.add_argument("--black", action="store_true",
                   help="play Black; the engine moves first")
    strength = p.add_mutually_exclusive_group()
    strength.add_argument("--elo", type=int, metavar="N",
                          help=f"engine strength, {Strength.ELO_MIN}-{Strength.ELO_MAX}")
    strength.add_argument("--skill", type=int, metavar="N",
                          help=f"engine skill level, {Strength.SKILL_MIN}-{Strength.SKILL_MAX} "
                               f"-- the only way below {Strength.ELO_MIN} Elo")
    p.add_argument("--depth", type=int, default=12, metavar="N",
                   help="engine search depth (default 12)")
    p.add_argument("--movetime", type=int, metavar="MS",
                   help="milliseconds per move, instead of a depth limit")
    p.add_argument("--threads", type=int, default=1, metavar="N",
                   help="engine threads (default 1)")
    p.add_argument("--fen", metavar="FEN", help="start from a position")

    lichess = p.add_argument_group("lichess (needs a board:play token)")
    lichess.add_argument("--lichess", action="store_true",
                         help="wait for a game to start on lichess.org and play it")
    lichess.add_argument("--lichess-ai", type=_ai_level, metavar="LEVEL",
                         help="challenge the Lichess AI, level 1-8, and play that")
    lichess.add_argument("--lichess-game", metavar="ID",
                         help="play a specific game already in progress")
    lichess.add_argument("--lichess-color", choices=("white", "black", "random"),
                         default="white", help="your colour when challenging the AI")
    lichess.add_argument("--token-file", metavar="PATH",
                         help="where to read the token (default ~/.lichess-token)")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    versus_engine = args.engine or args.black or args.elo is not None or args.skill is not None

    try:
        game = Game(args.fen)
    except ValueError as exc:
        print(f"bad FEN: {exc}", file=sys.stderr)
        return 2

    leds = ConsoleLEDDriver()
    keyboard = KeyboardInput()

    if args.lichess or args.lichess_ai is not None or args.lichess_game:
        return _play_lichess(args, leds)

    if not versus_engine:
        session = Session(game, keyboard, leds)
        keyboard._on_command = session.handle_command
        print("Local two-player. SAN or UCI; 'moves', 'takeback', 'fen', 'quit'.")
        try:
            session.run()
        finally:
            session.close()
        return 0

    try:
        strength = Strength(elo=args.elo, skill=args.skill, threads=args.threads)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    limit = (chess.engine.Limit(time=args.movetime / 1000.0) if args.movetime
             else chess.engine.Limit(depth=args.depth))

    try:
        engine = Engine(strength=strength, limit=limit)
    except EngineUnavailable as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    you = chess.BLACK if args.black else chess.WHITE
    engine_input = EngineInput(engine, announce=print, name=engine.name)
    session = Session(game, {you: keyboard, not you: engine_input}, leds)
    keyboard._on_command = session.handle_command

    print(f"You are {'Black' if args.black else 'White'} against {engine.name} "
          f"({strength.describe()}, {_limit_text(limit)}).")
    print("SAN or UCI; 'moves', 'takeback', 'fen', 'quit'.")
    try:
        session.run()
    finally:
        session.close()
    return 0


def _play_lichess(args, leds: ConsoleLEDDriver) -> int:
    try:
        client = Client(load_token(args.token_file))
        me = client.username()
    except LichessError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"Signed in as {me}.")

    try:
        if args.lichess_game:
            game_id = args.lichess_game
        elif args.lichess_ai is not None:
            created = client.challenge_ai(args.lichess_ai, color=args.lichess_color)
            game_id = created.get("id")
            print(f"  challenged the Lichess AI at level {args.lichess_ai}")
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
    game = LichessGame(client, game_id, leds, me)
    try:
        game.run()
    except LichessError as exc:
        print(f"\nlost the game stream: {exc}", file=sys.stderr)
        return 1
    return 0


def _limit_text(limit: chess.engine.Limit) -> str:
    if limit.time:
        return f"{int(limit.time * 1000)}ms/move"
    return f"depth {limit.depth}"
