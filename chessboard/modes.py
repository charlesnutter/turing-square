"""What kind of game to start, and how it was asked for.

`GameRequest` is the one description of a new game. The CLI builds one from
argparse, the API builds one from a JSON body, and both hand it to the same
resolver -- so a game started from the tablet cannot drift from a game started
from the terminal.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

LOCAL_HUMAN = "local-human"
LOCAL_AI = "local-ai"
ONLINE_HUMAN = "online-human"
ONLINE_AI = "online-ai"
MODES = (LOCAL_HUMAN, LOCAL_AI, ONLINE_HUMAN, ONLINE_AI)


@dataclass(frozen=True)
class GameRequest:
    mode: Optional[str] = None
    black: bool = False
    fen: Optional[str] = None
    engine: str = "stockfish"
    elo: Optional[int] = None
    skill: Optional[int] = None
    depth: int = 12
    movetime: Optional[int] = None
    threads: int = 1
    ai_level: Optional[int] = None
    game_id: Optional[str] = None
    color: str = "white"
    token_file: Optional[str] = None
    # CLI shorthand. The API sends a mode outright and leaves these alone; they
    # stay on the request so one resolver sees every way a mode can be implied.
    lichess: bool = False
    lichess_ai: Optional[int] = None


def resolve_mode(request: GameRequest) -> Tuple[Optional[str], Optional[str]]:
    """Work out the mode from an explicit choice or the options given.

    Returns (mode, error). Inference is a convenience; anything ambiguous is an
    error rather than a guess, because silently playing the wrong opponent is a
    worse outcome than being told to be explicit.
    """
    implied = []
    if request.lichess:
        implied.append(ONLINE_HUMAN)
    if (request.lichess_ai is not None or request.ai_level is not None
            or request.game_id):
        implied.append(ONLINE_AI if not request.game_id else ONLINE_HUMAN)
    if request.elo is not None or request.skill is not None:
        implied.append(LOCAL_AI)

    distinct = set(implied)
    if request.mode:
        conflicting = distinct - {request.mode}
        # --game is fine with either online mode.
        if conflicting and not (request.mode.startswith("online")
                                and conflicting <= {ONLINE_HUMAN, ONLINE_AI}):
            return None, (f"--mode {request.mode} conflicts with the other options "
                          f"given ({', '.join(sorted(conflicting))})")
        return request.mode, None

    if len(distinct) > 1:
        return None, ("those options imply more than one mode "
                      f"({', '.join(sorted(distinct))}) -- pass --mode explicitly")
    if distinct:
        return implied[0], None
    return LOCAL_HUMAN, None
