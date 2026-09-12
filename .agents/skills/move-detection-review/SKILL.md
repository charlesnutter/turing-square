---
name: move-detection-review
description: Review changes to the move matcher, the occupancy model, or their tests. Use whenever chessboard/core/matcher.py, chessboard/core/occupancy.py or tests/test_matcher.py is edited, or when adding a new move-detection case. Encodes the seven cases that must stay covered and the two occupancy collisions that are invisible to a bit-per-square sensor.
---

# Reviewing move detection

Move detection is the hardest part of this build and the place regressions hide,
because its failures are physical and intermittent rather than a failing import.
Work through this before calling a change to the matcher done.

## The rule the design rests on

The move is never derived from the occupancy diff. Every legal move is asked what
occupancy pattern it would produce, and the board is matched against that list.
If a change starts reasoning from *which squares changed* rather than *which
legal move produces this*, it is reintroducing the bug the design exists to
avoid.

## Two collisions are invisible to the sensors

Neither may ever be guessed at:

1. **The promotion piece.** Q/R/B/N leave identical occupancy. The UI asks.
2. **Two captures from one origin.** `Bxd2` and `Bxb2` from c3 both only empty
   c3 — each destination is occupied before *and* after, so the settled patterns
   are identical. Resolved from the lifted set, because the captured square reads
   empty for a moment. If that transient was never sampled, ask rather than pick.

## Checklist

- [ ] `pytest` passes — all of it, not just the file that changed.
- [ ] The seven cases are still covered: capture in **either** lift order,
      castling **both** sides mid-move, en passant, promotion, two captures from
      one origin, hover, and knockover.
- [ ] The randomised property test still runs, and still asserts that the only
      occupancy collisions are those two. **Do not weaken it to make a change
      pass** — it is what found the capture collision in the first place, and the
      hand-written cases did not.
- [ ] A move's footprint still includes the **destination square**. A capture
      does not change its destination between settled positions, so a footprint
      built from the before/after difference alone omits it — and then every
      capture in progress looks like a knocked-over piece.
- [ ] Time is injected, never read from the clock inside the matcher. No test
      sleeps.
- [ ] The matcher does not mutate the board. Committing the move is the game
      core's decision; `occupancy_after` must push and pop, leaving the board as
      it found it.
- [ ] MISMATCH is still stability-gated, so a single noisy frame cannot flash the
      board red mid-move.
- [ ] Any new state is both reachable and exitable. A state that blocks forever
      is worse than a wrong move.

## When adding a case

If the case is a *class* of position rather than a single one, write the property
test first. Forty random games found what six hand-written cases missed.
