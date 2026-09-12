# AGENTS.md

A magnet-sensing, LED-lit wooden chessboard. A Raspberry Pi 4 runs the game and
is the single source of truth; an Arduino Nano is dumb real-time I/O; a tablet
browser is a pure view. Three modes: over-the-board, versus a local engine, and
online via the Lichess Board API. Plus "suggest a move" and "explain why that
move was good or bad."

---

## Architecture invariants

These were argued through against datasheets, arithmetic and source. Treat them
as settled. If new information contradicts one, **say so explicitly** rather
than quietly switching.

- **`python-chess` owns the rules.** Legality, SAN, castling, en passant, PGN,
  the opening book (`chess.polyglot`) and tablebases (`chess.syzygy`). Stockfish
  is asked how *good* a move is, never whether it is *allowed*.
- **The Pi is the single source of truth.** The tablet sends commands and
  renders what it is pushed. The Arduino decides nothing.
- **Hardware lives behind two interfaces.** `LEDDriver` and `BoardInput`. The
  game core must never learn whether a move came from a keyboard or a magnet.
  Adding hardware changes one line of wiring-up code, never the core.
- **Move detection matches, it does not diff.** Generate the occupancy pattern
  every legal move would produce and match against that list. A raw diff cannot
  resolve captures, castling or en passant.
- **Facts first, prose second.** Classification, motifs and positional deltas
  are computed deterministically, then handed to a narrator that only writes the
  sentence. Correctness must never depend on a model. The narrator is a swappable
  interface: template, cloud, or local.
- **Maps are data, not code.** `led_map.json` and `sensor_map.json` are written
  by a calibration mode in the UI. Never hard-code square-to-pixel or
  square-to-bit order; the wiring will not match your assumptions.
- **Thresholds live in one config dict.** Move-classification bands are
  Lichess's (win-percentage delta .1 / .2 / .3). They cannot be evaluated until
  twenty games have been played on the finished board, so keep them in one place.

### Rejected — do not re-propose

Neo4j or any graph DB · Prolog · LangChain · Stockfish for legality · 74HC165
shift registers (MCP23017 instead) · Arduino Nano Every (FastLED's megaAVR
support is unreliable) · 12V pixel strings · drilling before Phase 3.

---

## Build order

Software (no hardware) → LEDs on cardboard → sensors on cardboard → woodworking
→ integration. Woodworking is the only irreversible phase, so it runs fourth.
Each phase ends in an **exit gate** that must pass before money is spent or cuts
are made. Do not pull hardware work forward to "save time."

**Phase 0 is current.** Done: game core, both driver interfaces, move matcher.
Remaining: versus-Stockfish, FastAPI + WebSocket, web UI, Lichess, and the
coach's fact layer.

---

## Tooling

- **Python 3.14**, venv at `.venv`. `pytest` for tests.
- Runtime deps are `chess`, `pyserial`, `fastapi`, `uvicorn` — that is the whole
  list, and it is meant to stay short.
- **Stockfish** from Homebrew, driven through `chess.engine`.
- Firmware is Arduino C++ with FastLED, on an ATmega328P Nano.
- Web UI is plain HTML and JS served by the Pi. No framework until something
  actually demands one.

### Dependency policy

**Do not add a runtime dependency without asking.** The standing preference is
to explain a method rather than install a package, and to *vendor or read* from
external projects rather than depend on them. Before writing classification or
motif code, check what has already been verified in the source-repo notes —
much of it exists and was read line by line.

---

## Licensing — settled, and it constrains how the coach gets written

This repo is **Apache-2.0 and public, and stays that way.**

`cook.py` from `ornicar/lichess-puzzler` is **AGPL-3.0**. Copyleft only flows one
way: permissive code can be absorbed into an AGPL work, but AGPL code can never
be redistributed under a permissive licence. So:

- **No AGPL-licensed code enters this repo.** Not vendored, not copied, not
  adapted line-by-line.
- **Reading `cook.py` for its logic is fine. Copying it is not.** The point of no
  return is the first copied line — untangling it afterwards means rewriting from
  scratch to prove clean-room, so stay on the right side of it from the start.
- The motif detectors get **written here**, over `python-chess`. Eight are worth
  writing (see below); the rest of Lichess's ~58 tags are either free from
  Stockfish, one-line queries, or puzzle-database bookkeeping that explains
  nothing to a player.

### The eight detectors

`hanging piece` · `fork` · `pin` (nearly free — `board.pin()` and
`board.is_pinned()` are built in) · `skewer` · `discovered attack` · `back-rank
vulnerability` · `trapped piece` · `defender removed`.

That last one is the important collapse: deflection, attraction, interference,
self-interference, clearance and capturing-defender are six separate tags in a
puzzle database, because someone filtering for deflection wants exactly
deflection. A coach explaining one move needs none of that discrimination — all
six produce the same sentence ("this pulls the rook off the back rank"). Compute
*"piece X no longer defends square Y"* once, phrase it once.

Most explanatory value is in the positional feature layer anyway (castling
rights, centre control, king shield, pawn structure, mobility, hanging
material), and nothing in `cook.py` computes any of it.

### The same rule applies to vendored skills

A skill copied into `.agents/skills/` is third-party files in this repo, under
its own licence. Vendored skills are **gitignored until their licence is
cleared** — using one locally is not redistribution, committing it is. The
tracked manifest at `.agents/skills/VENDORED.md` records what is in use, its
source, its licence and its status, and every row must be resolved before the
project ships.


---

## Testing

The move matcher is the hardest part of the project and the place bugs hide.

- **Write property tests, not just cases.** A randomised playout asserting that
  no two legal moves share an occupancy pattern found a collision that every
  hand-written case missed: two captures from the same origin (`Bxd2` / `Bxb2`
  from c3) leave identical settled occupancy. Keep that test, and reach for the
  same technique on the classifier and the positional features.
- **Inject time; never sleep in a test.** The stability window takes `now` as a
  parameter for exactly this reason.
- The move-detection suite must keep covering all seven cases: capture in either
  lift order, castling both sides mid-move, en passant, promotion, two captures
  from one origin, hover, and knockover.
- Run `pytest` before every commit.

---

## Firmware rules (Phase 1 onward)

Each of these prevents a fault that only appears after an hour of play, under a
board you have already sealed.

- **No Arduino `String` in `loop()`.** Fixed `char[64]` buffer, parse with
  `strtok`. Heap fragmentation is the usual cause of "it randomly freezes."
- **`delay()` is not a debounce.** Require a bit to hold its value for 150 ms
  before reporting it. `delay()` blocks incoming light commands and still lets
  sliding pieces generate ghost moves.
- **Handshake on boot.** `HELLO` + version, periodic `PING`/`PONG`, and the Pi
  resends a full frame on reconnect.
- **No busy-wait in the serial bridge.** The port has `timeout=0.1`; call
  `readline()` and let it block.
- **No hard-coded `/dev/ttyUSB0`.** A udev rule keyed to USB VID/PID gives a
  stable `/dev/chessboard`.
- **Never power pixels from the Nano's 5V pin,** and never write an "all white"
  test pattern — 256 pixels at full white is roughly 15 A against a 4 A supply.

---

## Secrets

- The Lichess token lives in a `0600` file on the Pi. Never in the frontend,
  never in this repo, never in a commit.
- **Never upgrade the Lichess account to a BOT account.** It is irreversible and
  requires an account with zero prior games. The Board API works fine with an
  ordinary account and a `board:play` token.
- `config/*.json` and `.env` are gitignored. Keep it that way.

---

## Keep AI tooling out of the repo

Nothing that references Claude, or any other AI assistant, belongs in version
control. This is a chess project and the repo should read as one.

Gitignored, and never to be committed:

- `CLAUDE.md` and `.claude/` — kept on disk so local tooling still works, but
  untracked
- `.cursor/`, `.cursorrules`, `.aider*`, `.github/copilot-*`, `.continue/`,
  `.windsurfrules`, and the equivalent for any other agent
- Any transcript, session log, prompt file or assistant scratch note

Also, and `.gitignore` cannot enforce either of these:

- **No attribution trailers in commit messages.** No `Co-Authored-By` naming an
  assistant, no session links. A commit message describes the change and nothing
  else.
- **Check new files by hand.** The ignore list catches the names known today; it
  will not catch whatever the next tool decides to write.

`AGENTS.md` is the deliberate exception. It stays tracked because it is project
documentation that any contributor or tool should read — which is the whole
point of the filename.

## Style

- Match the surrounding code. Comments explain *why*, not what — the existing
  modules set the density, follow it.
- Keep docstrings on the non-obvious parts (the matcher, the footprint rule);
  skip them on the obvious ones.
- Small, focused commits with a body explaining the reasoning when the change is
  not self-evident.
- Report honestly: if tests fail, say so with the output; if something was
  skipped, say that.

---

## Skills

Skills live in the vendor-neutral `.agents/skills/` location defined by the Agent
Skills format, and are symlinked into `.claude/skills/`, which is the only place
Claude Code looks. That keeps the repo portable across agents and free of
`Claude`-named paths; the symlinks are gitignored, so they never enter the repo.

```
.agents/skills/<name>/SKILL.md    the source of truth
.claude/skills/<name>             gitignored symlink → ../../.agents/skills/<name>
```

**After cloning, recreate the symlinks** — they are deliberately not tracked:

```sh
mkdir -p .claude/skills
for s in .agents/skills/*/; do
  ln -sfn "../../$s" ".claude/skills/$(basename "$s")"
done
```

### Written for this project — tracked

| Skill | Use it for |
|---|---|
| `move-detection-review` | Any change to the matcher, the occupancy model, or their tests |

Add to these when a mistake is worth not repeating. A skill is the right home for
a *procedure*; the Lessons section below is the right home for a *fact*.

### Vendored from the community — gitignored, licences pending

| Skill | Source | Licence |
|---|---|---|
| `test-driven-development` | obra/superpowers | MIT |

Kept deliberately small. See `.agents/skills/VENDORED.md` for the manifest, the
install-and-relocate flow, and what has to happen before the project ships.

Rejected after looking: `systematic-debugging` from the same repo ships eleven
files of TypeScript-flavoured examples, which is a poor fit here; the Arduino
skills on embeddedskills.dev are generic snippet generators with no stated
licence, and would happily emit code that breaks the firmware rules above.

### Built in — nothing to install

| Skill | Use it for |
|---|---|
| `/code-review` | Before each phase gate, and on the matcher and classifier especially |
| `/security-review` | Before the Lichess token handling and the FastAPI surface land |
| `/simplify` | Quality pass on changed code — no bug hunting |
| `/run` | Launching the app to confirm a change works for real |

### A note on plugins

The same content is often also distributed through vendor plugin marketplaces,
which install to `~/.claude/plugins/` — one machine, outside the repo, invisible
to any other agent. This project vendors instead, so the skills travel with a
clone and work with whatever tool reads them.

**Before adding any of it, check it earns its place.** The dependency policy
applies to tooling too, and a skill is instructions that become project policy
the moment it loads — read it before you trust it.

---

## Lessons — add to this when something bites

A running log of things that cost time once and should not cost it twice. Keep
entries short and concrete: what was assumed, what was true, what to do instead.

- **Write the property test, not just the cases.** Six hand-written move-detection
  cases all passed while the matcher was quietly wrong. Forty randomly played
  games asserting "no two legal moves share an occupancy pattern" found the
  same-origin capture collision in one run. When a case is a *class* of position,
  test the class.

- **A capture's destination square is touched, even though it does not change.**
  Occupied before, occupied after — so a footprint built from the before/after
  difference omits it, and every capture in progress reads as a knocked-over
  piece. Physical footprint ≠ state difference.

- **`python-chess`'s `parse_san` accepts UCI-shaped text.** It reads `e1e3` as a
  pawn move to e3 disambiguated from e1, then raises before a `Move` object
  exists — so illegal UCI never reached the core's explainer and players got a
  generic error. Match UCI with a regex first, and let the core explain.

- **Check where a tool writes before relying on it.** `npx skills add` installs to
  the agent's own directory — `.claude/skills/` for Claude Code — which is
  gitignored here. It would report success, work locally, and silently never
  commit. Verify the path, do not assume it.
