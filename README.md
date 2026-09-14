# Smart chessboard

A magnet-sensing, LED-lit wooden board that plays local, Stockfish, or Lichess.

Built software first: everything here runs on a laptop with no Pi, no Arduino
and no wires. Hardware arrives behind two interfaces, so Phases 1 and 2 add an
implementation and change one line of wiring-up code each.

## Running it

```sh
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install stockfish
.venv/bin/python -m pytest
```

Play in the terminal:

```sh
.venv/bin/python -m chessboard                              # two players, one keyboard
.venv/bin/python -m chessboard --mode local-ai --elo 1500   # versus Stockfish
.venv/bin/python -m chessboard --mode local-ai --skill 2 --black
.venv/bin/python -m chessboard --mode online-human          # a person on lichess.org
.venv/bin/python -m chessboard --mode online-ai --ai-level 1
```

Or serve it, and play from a browser:

```sh
.venv/bin/python -m chessboard --serve --host 0.0.0.0
```

Type moves as SAN (`Nf3`) or UCI (`g1f3`); `moves`, `takeback`, `fen`, `board`,
`quit`. Run `--help` for the full set.

> **The online modes create a real game on the Lichess account as soon as they
> run.** They need a `board:play` token in `~/.lichess-token`, mode `0600`.
> `--game ID` joins an existing game without creating one.

## Layout

```
chessboard/
  core/
    game.py        rules, state, SAN history, PGN, why-that-is-illegal
    engine.py      ChessEngine interface + StockfishEngine
    matcher.py     occupancy readings -> moves (the hard part)
    occupancy.py   bit-per-square helpers over python-chess bitboards
  drivers/
    led.py         LEDDriver + ConsoleLEDDriver      (SerialLEDDriver in phase 1)
    board_input.py BoardInput + KeyboardInput        (SerialBoardInput in phase 2)
  lichess/
    client.py      Board API over the standard library
    play.py        one Lichess game, driven by the event bus
  api/
    state.py       the snapshot the client renders
    service.py     HTTP requests onto the game loop's queue, and back
    app.py         the routes
  events.py        one queue, several producers
  session.py       one local game, driven by events
  modes.py         GameRequest + mode resolution, shared by the CLI and the API
  cli.py           choose a mode, wire it up, run it
```

`python-chess` owns legality, SAN, castling, en passant and PGN. Stockfish is
asked how good a move is, never whether it is allowed.

## Everything is a producer on one queue

A keyboard, an engine, a Lichess stream, an HTTP request and — from Phase 2 —
64 magnets all produce moves, and none of them may block the others. Each runs
as a producer on a shared queue; one loop owns the board and decides what each
event means against the current position.

The alternative is what this started as: asking one source for the next move and
blocking there. That made moves played in a browser invisible while the terminal
waited for a typed line, and it leaves no loop for an HTTP request to inject
into. An engine is the one exception — it has nothing to say until it is asked,
so it is asked on its own thread and posts its answer back.

## The API

`--serve` puts the same game behind HTTP. Commands in over REST, state out over
a WebSocket; the client renders what it is pushed and decides nothing.

| | |
|---|---|
| `GET /api/state` | the current state, or `{"running": false}` |
| `POST /api/game` | start one — the `GameRequest` fields as JSON |
| `DELETE /api/game` | end it |
| `POST /api/move` | `{"move": "e4"}`, SAN or UCI |
| `POST /api/command` | `{"command": "takeback"}` |
| `WS /ws` | every state change, pushed |

The push is the half that matters: a move made on the physical board, or by an
engine nobody asked over HTTP, has no request to answer.

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
