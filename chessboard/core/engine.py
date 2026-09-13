"""Local engines.

An engine answers "what would you play?" and "how good is this?". It is never
asked whether a move is legal -- python-chess owns that.

`ChessEngine` is the interface; `StockfishEngine` is the only implementation
today. The abstraction exists because Stockfish at low skill plays *inhuman*
moves -- strong ones interspersed with bizarre ones -- which is unsatisfying
across a physical board. Maia (nine nets trained on human games, 1100-1900 Elo,
run under lc0 with search disabled) is the planned fix, and it has no
`UCI_Elo` or `Skill Level` at all: you pick a net. So strength configuration
belongs to the implementation, not to the interface.

Adding an engine must not add a play mode. `--mode local-ai --engine maia` is
the shape; a separate "play Maia" mode is not.
"""

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import chess
import chess.engine

DEFAULT_PATH = "stockfish"


class EngineUnavailable(RuntimeError):
    """No Stockfish binary on PATH, or it would not start."""


@dataclass(frozen=True)
class Strength:
    """How hard the engine tries.

    Two mechanisms, and they are *not* interchangeable. `UCI_Elo` bottoms out at
    1320, so anything weaker than a decent club player has to go through
    `Skill Level` instead. Ranges probed from Stockfish 19 and re-checked against
    the running binary at configure time, because they are build-dependent.
    """

    elo: Optional[int] = None
    skill: Optional[int] = None
    threads: int = 1
    hash_mb: int = 16

    ELO_MIN = 1320
    ELO_MAX = 3190
    SKILL_MIN = 0
    SKILL_MAX = 20

    def __post_init__(self) -> None:
        if self.elo is not None and self.skill is not None:
            raise ValueError(
                "set elo or skill, not both -- they are separate mechanisms"
            )
        if self.elo is not None and not self.ELO_MIN <= self.elo <= self.ELO_MAX:
            raise ValueError(
                f"UCI_Elo accepts {self.ELO_MIN}-{self.ELO_MAX}. "
                f"For weaker play use skill ({self.SKILL_MIN}-{self.SKILL_MAX})."
            )
        if self.skill is not None and not self.SKILL_MIN <= self.skill <= self.SKILL_MAX:
            raise ValueError(
                f"Skill Level accepts {self.SKILL_MIN}-{self.SKILL_MAX}"
            )
        if self.threads < 1:
            raise ValueError("threads must be at least 1")

    def as_options(self) -> dict:
        options: dict = {"Threads": self.threads, "Hash": self.hash_mb}
        if self.elo is not None:
            options["UCI_LimitStrength"] = True
            options["UCI_Elo"] = self.elo
        elif self.skill is not None:
            options["Skill Level"] = self.skill
        return options

    def describe(self) -> str:
        if self.elo is not None:
            return f"~{self.elo} Elo"
        if self.skill is not None:
            return f"skill {self.skill}/{self.SKILL_MAX}"
        return "full strength"


FULL_STRENGTH = Strength()


class ChessEngine(ABC):
    """Anything that can choose a move.

    `EngineInput` duck-types on `play`, so an engine need not inherit from this
    to be usable -- but implementing it documents the contract and gets the
    context-manager behaviour for free.
    """

    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def play(self, board: chess.Board) -> chess.Move:
        """The move this engine would make. Never consulted about legality."""

    def analyse(self, board: chess.Board, multipv: int = 1, limit=None) -> list:
        """Top lines with scores. Engines that cannot evaluate return nothing."""
        return []

    def describe(self) -> str:
        return self.name

    def close(self) -> None:
        pass

    def __enter__(self) -> "ChessEngine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class StockfishEngine(ChessEngine):
    """A running Stockfish process.

    Use as a context manager so the process is always reaped:

        with StockfishEngine(strength=Strength(elo=1500)) as engine:
            move = engine.play(board)
    """

    def __init__(self, path: str = DEFAULT_PATH,
                 strength: Optional[Strength] = None,
                 limit: Optional[chess.engine.Limit] = None):
        if shutil.which(path) is None:
            raise EngineUnavailable(
                f"{path!r} is not on PATH. Install it with `brew install stockfish`."
            )
        self.path = path
        self.strength = strength or FULL_STRENGTH
        self.limit = limit or chess.engine.Limit(depth=12)
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(path)
        except Exception as exc:  # noqa: BLE001 -- surface whatever went wrong
            raise EngineUnavailable(f"could not start {path!r}: {exc}") from exc
        self._configure()

    @staticmethod
    def available(path: str = DEFAULT_PATH) -> bool:
        return shutil.which(path) is not None

    @property
    def name(self) -> str:
        return self._engine.id.get("name", "unknown engine")

    def _configure(self) -> None:
        """Apply strength, checking each option against the running binary.

        Option names and ranges differ between builds, so nothing is assumed --
        an unsupported option is a clear error rather than a silently ignored
        setting that leaves the engine at full strength.
        """
        options = self.strength.as_options()
        for name, value in options.items():
            declared = self._engine.options.get(name)
            if declared is None:
                raise EngineUnavailable(
                    f"{self.name} does not support the UCI option {name!r}"
                )
            if declared.type == "spin" and declared.min is not None:
                if not declared.min <= value <= declared.max:
                    raise EngineUnavailable(
                        f"{name}={value} is outside {self.name}'s range "
                        f"{declared.min}-{declared.max}"
                    )
        self._engine.configure(options)

    def play(self, board: chess.Board) -> chess.Move:
        """The move the engine would make. Never consulted about legality."""
        if board.is_game_over():
            raise ValueError("asked the engine to move in a finished game")
        result = self._engine.play(board, self.limit)
        if result.move is None:
            raise RuntimeError(
                "engine returned no move -- it resigned or offered a draw"
            )
        return result.move

    def analyse(self, board: chess.Board, multipv: int = 1,
                limit: Optional[chess.engine.Limit] = None) -> list:
        """Top `multipv` lines with scores. The fact layer will want this."""
        if board.is_game_over():
            return []
        info = self._engine.analyse(board, limit or self.limit, multipv=multipv)
        return info if isinstance(info, list) else [info]

    def score_cp(self, board: chess.Board) -> Optional[int]:
        """Evaluation in centipawns from White's point of view, or None at mate."""
        info = self.analyse(board)
        if not info:
            return None
        return info[0]["score"].white().score()

    def close(self) -> None:
        try:
            self._engine.quit()
        except Exception:  # noqa: BLE001 -- closing must never raise
            pass

    def describe(self) -> str:
        return f"{self.name} ({self.strength.describe()})"


ENGINES = {"stockfish": StockfishEngine}


def open_engine(kind: str = "stockfish", **kwargs) -> ChessEngine:
    """Open a local engine by name. The registry is where Maia will land."""
    try:
        factory = ENGINES[kind]
    except KeyError:
        raise EngineUnavailable(
            f"unknown engine {kind!r} -- known: {', '.join(sorted(ENGINES))}"
        ) from None
    return factory(**kwargs)
