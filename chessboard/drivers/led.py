"""The LED output boundary.

The game core lights squares; it never learns whether a square is a pixel on a
strip or a line of text. Phase 1 adds SerialLEDDriver next to ConsoleLEDDriver
and changes one line of wiring-up code.
"""

from abc import ABC, abstractmethod
from typing import Iterable, Mapping, NamedTuple

import chess


class Color(NamedTuple):
    r: int
    g: int
    b: int


# Semantic colours. Brightness is capped in firmware, not here -- 256 pixels at
# full white is roughly 15 A against a 4 A supply.
OFF = Color(0, 0, 0)
GREEN = Color(0, 180, 60)
RED = Color(200, 30, 30)
AMBER = Color(190, 120, 0)
BLUE = Color(30, 90, 200)


class LEDDriver(ABC):
    """One pixel per square, addressed by python-chess square index (a1 = 0)."""

    @abstractmethod
    def set(self, square: int, color: Color) -> None:
        """Stage one square. Not visible until show()."""

    @abstractmethod
    def show(self) -> None:
        """Push the staged frame to the board."""

    def set_many(self, frame: Mapping[int, Color]) -> None:
        for square, color in frame.items():
            self.set(square, color)

    def clear(self) -> None:
        for square in chess.SQUARES:
            self.set(square, OFF)

    def light_only(self, squares: Iterable[int], color: Color) -> None:
        """The common case: clear everything, light these, push."""
        self.clear()
        for square in squares:
            self.set(square, color)
        self.show()

    def close(self) -> None:
        pass


class ConsoleLEDDriver(LEDDriver):
    """Phase 0. Prints `e2 -> green` instead of lighting anything."""

    NAMES = {OFF: "off", GREEN: "green", RED: "red", AMBER: "amber", BLUE: "blue"}

    def __init__(self, echo=print, quiet_off: bool = True):
        self._echo = echo
        self._quiet_off = quiet_off
        self._frame: dict[int, Color] = {}
        self._shown: dict[int, Color] = {}

    def set(self, square: int, color: Color) -> None:
        self._frame[square] = color

    def show(self) -> None:
        changed = {sq: c for sq, c in self._frame.items() if self._shown.get(sq, OFF) != c}
        self._shown = dict(self._frame)
        lit = [(sq, c) for sq, c in sorted(changed.items()) if not (self._quiet_off and c == OFF)]
        if not lit:
            return
        self._echo("  LED  " + "  ".join(
            f"{chess.square_name(sq)} -> {self.NAMES.get(c, str(tuple(c)))}" for sq, c in lit
        ))
