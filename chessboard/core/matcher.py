"""Move detection: match, don't diff.

The sensors report which squares are occupied, never which piece is on them. A
raw before/after diff cannot resolve a capture (two events on one square), a
castle (two pieces, four transitions, arbitrary order) or an en passant (the
captured pawn is not on the destination square), and it cannot tell a finished
move from a piece held in mid-air.

So the move is never derived from the diff. Instead every legal move is asked
what occupancy pattern it would produce, and the board is matched against that
list. The rules of chess do the disambiguating.

Two facts make this work:

  * The settled occupancy pattern almost always identifies the move. There are
    exactly two exceptions, both of them invisible to a bit-per-square sensor:
    the choice of promotion piece, and two captures leaving the same origin
    (Bxd2 and Bxb2 from c3 both just empty c3 -- each destination is occupied
    before and after). Neither is guessed at.

  * A half-finished move is recognisable: every square that differs from the
    settled position lies inside some legal move's footprint. A knocked-over
    piece does not, which is what separates "still moving" from "broken".

The second exception is resolved by watching, not by reasoning. A captured
piece is lifted off before the capturing piece lands, so its square reads empty
for a moment. Tracking which squares have gone empty since the board was last
settled -- the lifted set -- picks the right capture out of the pair. If that
transient is missed, the matcher asks rather than guessing.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import chess

from .occupancy import footprint, names, occupancy_after


class Phase(Enum):
    SETTLED = "settled"        # board agrees with the engine; nothing to do
    LIFTED = "lifted"          # pieces up, none placed yet
    PENDING = "pending"        # in motion, or waiting out the stability window
    COMMITTED = "committed"    # exactly one legal move, held steady
    PROMOTION = "promotion"    # move is known, promotion piece must be asked for
    AMBIGUOUS = "ambiguous"    # two captures from one origin; ask which
    MISMATCH = "mismatch"      # no legal move explains this; block until fixed


@dataclass(frozen=True)
class MatchResult:
    phase: Phase
    move: Optional[chess.Move] = None
    choices: tuple[chess.Move, ...] = ()
    diff: int = 0
    candidates: tuple[chess.Move, ...] = field(default=(), repr=False)

    @property
    def diff_squares(self) -> list[str]:
        return names(self.diff)

    def __str__(self) -> str:
        if self.phase is Phase.COMMITTED:
            return f"committed {self.move}"
        if self.phase in (Phase.PROMOTION, Phase.AMBIGUOUS):
            return f"{self.phase.value}: " + "/".join(m.uci() for m in self.choices)
        if self.diff:
            return f"{self.phase.value} {','.join(self.diff_squares)}"
        return self.phase.value


class MoveMatcher:
    """Turns a stream of occupancy readings into moves.

    Holds a reference to the caller's board and never mutates it -- committing
    the move is the game core's decision, not the sensor layer's. Once the core
    pushes, the matcher's idea of "settled" moves with it automatically.

    `now` is passed in rather than read from the clock so the stability window
    is testable without sleeping.
    """

    def __init__(self, board: chess.Board, stable_ms: int = 150):
        self.board = board
        self.stable_s = stable_ms / 1000.0
        self._held: Optional[int] = None
        self._held_since: float = 0.0
        self._vacated: int = 0

    def reset(self) -> None:
        self._held = None
        self._held_since = 0.0
        self._vacated = 0

    def _needs_vacating(self, move: chess.Move) -> int:
        """Squares this move can only have used by emptying them on the way.

        For an ordinary capture that is the destination: occupied before,
        occupied after, and necessarily empty in between. For everything else
        it is nothing at all.
        """
        return (footprint(self.board, move)
                & self.board.occupied
                & occupancy_after(self.board, move))

    def _stable(self, occupancy: int, now: float) -> bool:
        """True once this exact pattern has held for the full window."""
        if occupancy != self._held:
            self._held = occupancy
            self._held_since = now
            return False
        return (now - self._held_since) >= self.stable_s

    def observe(self, occupancy: int, now: float) -> MatchResult:
        expected = self.board.occupied
        delta = occupancy ^ expected

        if delta == 0:
            self.reset()
            return MatchResult(Phase.SETTLED)

        # Anything that has gone empty since we were last settled stays on the
        # record -- it is what tells two captures from one origin apart.
        self._vacated |= expected & ~occupancy

        legal = list(self.board.legal_moves)
        complete = [m for m in legal if occupancy_after(self.board, m) == occupancy]

        if complete:
            stable = self._stable(occupancy, now)

            witnessed = [m for m in complete if not self._needs_vacating(m) & ~self._vacated]
            # If the lift was too quick to sample, fall back rather than
            # rejecting a move that plainly happened.
            pool = witnessed or complete

            if len(pool) == 1:
                if not stable:
                    return MatchResult(Phase.PENDING, candidates=tuple(pool))
                return MatchResult(Phase.COMMITTED, move=pool[0], candidates=tuple(pool))

            # Same origin and destination, differing only in promotion piece.
            one_square = len({(m.from_square, m.to_square) for m in pool}) == 1
            phase = Phase.PROMOTION if (one_square and all(m.promotion for m in pool)) \
                else Phase.AMBIGUOUS
            if not stable:
                return MatchResult(Phase.PENDING, candidates=tuple(pool))
            return MatchResult(phase, choices=tuple(pool), candidates=tuple(pool))

        # Not a finished move. Is it a plausible half-finished one? Every square
        # that has changed must be one that some legal move was going to touch.
        in_progress = [m for m in legal if delta & ~footprint(self.board, m) == 0]

        if in_progress:
            self._held = None
            self._held_since = 0.0
            if delta & occupancy == 0:  # every change is a removal
                return MatchResult(Phase.LIFTED, diff=delta, candidates=tuple(in_progress))
            return MatchResult(Phase.PENDING, diff=delta, candidates=tuple(in_progress))

        # Nothing explains this. Wait out the window before crying foul, so a
        # noisy sensor reading does not flash the board red mid-move.
        if not self._stable(occupancy, now):
            return MatchResult(Phase.PENDING, diff=delta)
        return MatchResult(Phase.MISMATCH, diff=delta)
