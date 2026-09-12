"""The six cases that break naive move detection, plus the property the whole
design rests on.

Timings are in seconds and passed in explicitly; nothing here sleeps.
"""

import random

import chess
import pytest

from chessboard.core.matcher import MoveMatcher, Phase
from chessboard.core.occupancy import footprint, occupancy_after

STABLE_MS = 150
LONG = 0.5  # comfortably past the stability window


def setup(fen=None):
    board = chess.Board(fen) if fen else chess.Board()
    return board, MoveMatcher(board, stable_ms=STABLE_MS)


def hold(matcher, occupancy, t=0.0):
    """Observe one frame twice, as a polling loop would, so it can go stable."""
    matcher.observe(occupancy, now=t)
    return matcher.observe(occupancy, now=t + LONG)


def lift(occupancy, *names):
    for name in names:
        occupancy &= ~chess.BB_SQUARES[chess.parse_square(name)]
    return occupancy


def place(occupancy, *names):
    for name in names:
        occupancy |= chess.BB_SQUARES[chess.parse_square(name)]
    return occupancy


# --------------------------------------------------------------------------
# baseline
# --------------------------------------------------------------------------

def test_settled_when_the_board_agrees_with_the_engine():
    board, m = setup()
    assert m.observe(board.occupied, now=0.0).phase is Phase.SETTLED


def test_lift_then_place_commits_the_move():
    board, m = setup()
    settled = board.occupied

    assert m.observe(lift(settled, "e2"), now=0.0).phase is Phase.LIFTED

    target = occupancy_after(board, chess.Move.from_uci("e2e4"))
    assert m.observe(target, now=0.1).phase is Phase.PENDING

    result = m.observe(target, now=0.1 + LONG)
    assert result.phase is Phase.COMMITTED
    assert result.move == chess.Move.from_uci("e2e4")


def test_commit_waits_out_the_stability_window():
    """A piece sliding across a square must not register as a move."""
    board, m = setup()
    target = occupancy_after(board, chess.Move.from_uci("e2e4"))

    assert m.observe(target, now=0.0).phase is Phase.PENDING
    assert m.observe(target, now=0.05).phase is Phase.PENDING
    assert m.observe(target, now=0.149).phase is Phase.PENDING
    assert m.observe(target, now=0.150).phase is Phase.COMMITTED


# --------------------------------------------------------------------------
# hover -- lifted in thought, put back
# --------------------------------------------------------------------------

def test_hover_never_becomes_a_move():
    board, m = setup()
    settled = board.occupied

    assert m.observe(lift(settled, "g1"), now=0.0).phase is Phase.LIFTED
    assert m.observe(lift(settled, "g1"), now=LONG).phase is Phase.LIFTED
    assert m.observe(settled, now=2 * LONG).phase is Phase.SETTLED
    assert board.move_stack == []


# --------------------------------------------------------------------------
# capture -- two events on one square, either order
# --------------------------------------------------------------------------

CAPTURE_FEN = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


@pytest.mark.parametrize("order", [("e4", "d5"), ("d5", "e4")])
def test_capture_in_either_lift_order(order):
    board, m = setup(CAPTURE_FEN)
    settled = board.occupied
    exd5 = chess.Move.from_uci("e4d5")

    assert m.observe(lift(settled, order[0]), now=0.0).phase in (Phase.LIFTED, Phase.PENDING)
    assert m.observe(lift(settled, *order), now=0.1).phase is Phase.LIFTED

    result = hold(m, occupancy_after(board, exd5), t=0.2)
    assert result.phase is Phase.COMMITTED
    assert result.move == exd5


def test_capture_destination_is_inside_the_footprint():
    """The square being captured on goes low and high again mid-move."""
    board, _ = setup(CAPTURE_FEN)
    mask = footprint(board, chess.Move.from_uci("e4d5"))
    assert mask & chess.BB_SQUARES[chess.D5]
    assert mask & chess.BB_SQUARES[chess.E4]


# --------------------------------------------------------------------------
# castling -- two pieces, four transitions, arbitrary order
# --------------------------------------------------------------------------

CASTLE_FEN = "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1"


@pytest.mark.parametrize(
    "uci,king_from,king_to,rook_from,rook_to",
    [("e1g1", "e1", "g1", "h1", "f1"), ("e1c1", "e1", "c1", "a1", "d1")],
)
def test_castling_is_never_a_mismatch_midway(uci, king_from, king_to, rook_from, rook_to):
    board, m = setup(CASTLE_FEN)
    settled = board.occupied
    move = chess.Move.from_uci(uci)

    # both pieces up
    assert m.observe(lift(settled, king_from, rook_from), now=0.0).phase is Phase.LIFTED

    # king down, rook still in hand -- the state a diff-based matcher rejects
    midway = place(lift(settled, king_from, rook_from), king_to)
    for t in (0.1, 0.4, 1.0):
        assert m.observe(midway, now=t).phase is Phase.PENDING

    result = hold(m, occupancy_after(board, move), t=2.0)
    assert result.phase is Phase.COMMITTED
    assert result.move == move


# --------------------------------------------------------------------------
# en passant -- the captured pawn is not on the destination square
# --------------------------------------------------------------------------

EP_FEN = "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2"


def test_en_passant():
    board, m = setup(EP_FEN)
    settled = board.occupied
    move = chess.Move.from_uci("e5d6")
    assert board.is_en_passant(move)

    assert m.observe(lift(settled, "e5"), now=0.0).phase is Phase.LIFTED
    assert m.observe(lift(settled, "e5", "d5"), now=0.1).phase is Phase.LIFTED

    result = hold(m, occupancy_after(board, move), t=0.2)
    assert result.phase is Phase.COMMITTED
    assert result.move == move


# --------------------------------------------------------------------------
# promotion -- a piece swap the sensors cannot see
# --------------------------------------------------------------------------

PROMO_FEN = "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"


def test_promotion_asks_instead_of_guessing():
    board, m = setup(PROMO_FEN)
    target = occupancy_after(board, chess.Move.from_uci("a7a8q"))

    assert m.observe(target, now=0.0).phase is Phase.PENDING
    result = m.observe(target, now=LONG)

    assert result.phase is Phase.PROMOTION
    assert result.move is None
    assert {m_.promotion for m_ in result.choices} == {
        chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT
    }
    assert {m_.from_square for m_ in result.choices} == {chess.A7}
    assert {m_.to_square for m_ in result.choices} == {chess.A8}


# --------------------------------------------------------------------------
# knockover -- nothing legal explains it
# --------------------------------------------------------------------------

def test_knocked_over_piece_blocks_with_mismatch():
    board, m = setup()
    settled = board.occupied
    # a black piece topples from d7 onto d5, on White's move
    broken = place(lift(settled, "d7"), "d5")

    assert m.observe(broken, now=0.0).phase is Phase.PENDING  # waits out the window
    result = m.observe(broken, now=LONG)

    assert result.phase is Phase.MISMATCH
    assert sorted(result.diff_squares) == ["d5", "d7"]
    assert board.move_stack == []


def test_mismatch_clears_when_the_pieces_go_back():
    board, m = setup()
    settled = board.occupied
    broken = place(lift(settled, "d7"), "d5")

    assert hold(m, broken).phase is Phase.MISMATCH
    assert m.observe(settled, now=3 * LONG).phase is Phase.SETTLED


def test_noise_does_not_flash_red():
    """One bad frame between good ones must not reach MISMATCH."""
    board, m = setup()
    settled = board.occupied
    glitch = place(lift(settled, "d7"), "d5")

    assert m.observe(glitch, now=0.0).phase is Phase.PENDING
    assert m.observe(settled, now=0.02).phase is Phase.SETTLED


# --------------------------------------------------------------------------
# the property the design rests on
# --------------------------------------------------------------------------

def assert_occupancy_identifies_the_move(board):
    groups: dict[int, list[chess.Move]] = {}
    for move in board.legal_moves:
        groups.setdefault(occupancy_after(board, move), []).append(move)

    for occupancy, moves in groups.items():
        if len(moves) == 1:
            continue
        # Only two kinds of collision exist, and a bit-per-square sensor can
        # see neither: the promotion piece, and two captures from one origin
        # (each destination is occupied before and after, so only the origin
        # actually changes). Anything else would be a hole in the design.
        assert len({m.from_square for m in moves}) == 1, (
            f"{board.fen()} -- {[m.uci() for m in moves]} share an occupancy pattern"
        )
        if len({m.to_square for m in moves}) > 1:
            assert all(board.is_capture(m) for m in moves), (
                f"{board.fen()} -- {[m.uci() for m in moves]} collide but are not all captures"
            )
        else:
            assert all(m.promotion for m in moves)


@pytest.mark.parametrize("fen", [None, CAPTURE_FEN, CASTLE_FEN, EP_FEN, PROMO_FEN])
def test_occupancy_identifies_the_move(fen):
    assert_occupancy_identifies_the_move(chess.Board(fen) if fen else chess.Board())


def test_occupancy_identifies_the_move_across_random_games():
    rng = random.Random(20260912)
    for _ in range(40):
        board = chess.Board()
        for _ in range(rng.randint(0, 80)):
            if board.is_game_over():
                break
            assert_occupancy_identifies_the_move(board)
            board.push(rng.choice(list(board.legal_moves)))


# --------------------------------------------------------------------------
# two captures from one origin -- the collision the random playout found
# --------------------------------------------------------------------------

TWO_CAPTURES_FEN = "1rbqk2r/nppp2p1/p5n1/1B2pP2/7p/2b1N2P/PPPPNPP1/R1BQKR2 b - - 10 16"


def test_two_captures_from_one_origin_share_an_occupancy_pattern():
    """Bxd2 and Bxb2 are indistinguishable once the dust settles."""
    board = chess.Board(TWO_CAPTURES_FEN)
    bxd2 = chess.Move.from_uci("c3d2")
    bxb2 = chess.Move.from_uci("c3b2")
    assert board.is_capture(bxd2) and board.is_capture(bxb2)
    assert occupancy_after(board, bxd2) == occupancy_after(board, bxb2)


@pytest.mark.parametrize("uci,taken", [("c3d2", "d2"), ("c3b2", "b2")])
def test_the_lifted_set_tells_the_two_captures_apart(uci, taken):
    board, m = setup(TWO_CAPTURES_FEN)
    settled = board.occupied
    move = chess.Move.from_uci(uci)

    # bishop up, then the captured pawn off the board
    m.observe(lift(settled, "c3"), now=0.0)
    m.observe(lift(settled, "c3", taken), now=0.1)

    result = hold(m, occupancy_after(board, move), t=0.2)
    assert result.phase is Phase.COMMITTED
    assert result.move == move


def test_unwitnessed_capture_asks_instead_of_guessing():
    """If the captured square is never sampled empty, do not pick one at random."""
    board, m = setup(TWO_CAPTURES_FEN)
    settled = board.occupied

    m.observe(lift(settled, "c3"), now=0.0)  # bishop up, nothing else seen
    result = hold(m, occupancy_after(board, chess.Move.from_uci("c3d2")), t=0.1)

    assert result.phase is Phase.AMBIGUOUS
    assert result.move is None
    assert {mv.uci() for mv in result.choices} == {"c3d2", "c3b2"}
