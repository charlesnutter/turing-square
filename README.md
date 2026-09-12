# Smart chessboard

A magnet-sensing, LED-lit wooden board that plays local, Stockfish, or Lichess.

Built software first: everything here runs on a laptop with no Pi, no Arduino
and no wires. Hardware arrives behind two interfaces, so Phases 1 and 2 add an
implementation and change one line of wiring-up code each.

Build plan, parts list and phase checklist:
<https://claude.ai/code/artifact/ecbe4ac4-fc03-425d-8641-4b2c3a6ecacd>

## Running it

```sh
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install stockfish
.venv/bin/python -m chessboard     # local two-player, from the keyboard
.venv/bin/python -m pytest         # the move matcher's edge cases
```

## Layout

```
chessboard/
  core/
    game.py        rules, state, SAN history, PGN, why-that-is-illegal
    matcher.py     occupancy readings -> moves (the hard part)
    occupancy.py   bit-per-square helpers over python-chess bitboards
  drivers/
    led.py         LEDDriver + ConsoleLEDDriver      (SerialLEDDriver in phase 1)
    board_input.py BoardInput + KeyboardInput        (SerialBoardInput in phase 2)
  session.py       wires a Game to whichever drivers it is handed
```

`python-chess` owns legality, SAN, castling, en passant and PGN. Stockfish is
asked how good a move is, never whether it is allowed.

## Move detection

The sensors give one bit per square and cannot tell which piece is where. A
before/after diff cannot resolve a capture, a castle or an en passant, so the
move is never derived from the diff: every legal move is asked what occupancy
pattern it would produce, and the board is matched against that list.

Two collisions survive that, both genuinely invisible to a bit-per-square
sensor, and neither is guessed at:

- **The promotion piece.** Q/R/B/N leave identical occupancy — the UI asks.
- **Two captures from one origin.** `Bxd2` and `Bxb2` from c3 both only empty
  c3; each destination is occupied before *and* after. Resolved by watching the
  captured square go momentarily empty (the lifted set). If that transient is
  missed, the UI asks rather than picking one.

`MISMATCH` is a feature, not an error: when no legal move explains the board,
the differing squares light red and play blocks until they are put back.
