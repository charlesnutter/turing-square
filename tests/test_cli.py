"""Mode resolution.

Four modes in the UI, two implementations behind them. Inference from flags is a
convenience; anything ambiguous must be an error, because silently playing the
wrong opponent is worse than being told to be explicit.
"""

import pytest

from chessboard.cli import build_parser, request_from_args
from chessboard.modes import (
    LOCAL_AI, LOCAL_HUMAN, ONLINE_AI, ONLINE_HUMAN, resolve_mode,
)


def mode_for(*argv):
    """Parse, adapt to a GameRequest, resolve -- the path `main` actually takes."""
    args = build_parser().parse_args(list(argv))
    if args.lichess_ai is not None and args.ai_level is None:
        args.ai_level = args.lichess_ai
    return resolve_mode(request_from_args(args))


@pytest.mark.parametrize("argv,expected", [
    ([], LOCAL_HUMAN),
    (["--mode", "local-human"], LOCAL_HUMAN),
    (["--mode", "local-ai"], LOCAL_AI),
    (["--mode", "online-human"], ONLINE_HUMAN),
    (["--mode", "online-ai"], ONLINE_AI),
])
def test_explicit_mode_is_taken_as_given(argv, expected):
    assert mode_for(*argv) == (expected, None)


@pytest.mark.parametrize("argv,expected", [
    (["--elo", "1500"], LOCAL_AI),
    (["--skill", "3"], LOCAL_AI),
    (["--lichess"], ONLINE_HUMAN),
    (["--lichess-ai", "2"], ONLINE_AI),
    (["--ai-level", "4"], ONLINE_AI),
    (["--game", "abcd1234"], ONLINE_HUMAN),
])
def test_mode_is_inferred_when_unambiguous(argv, expected):
    assert mode_for(*argv) == (expected, None)


def test_defaults_to_two_players():
    assert mode_for() == (LOCAL_HUMAN, None)


def test_contradictory_options_are_refused_not_guessed():
    mode, error = mode_for("--elo", "1500", "--lichess")
    assert mode is None
    assert "more than one mode" in error


def test_explicit_mode_conflicting_with_options_is_refused():
    mode, error = mode_for("--mode", "local-ai", "--lichess")
    assert mode is None
    assert "conflicts" in error


def test_joining_a_game_works_with_either_online_mode():
    """--game names a game; it does not say who is on the other side."""
    assert mode_for("--mode", "online-ai", "--game", "abcd1234")[0] == ONLINE_AI
    assert mode_for("--mode", "online-human", "--game", "abcd1234")[0] == ONLINE_HUMAN


def test_black_alone_does_not_imply_an_opponent():
    """Playing Black is a seat, not a mode -- two players can still swap sides."""
    assert mode_for("--black") == (LOCAL_HUMAN, None)


@pytest.mark.parametrize("level", ["0", "9", "banana"])
def test_ai_level_is_rejected_at_parse_time(level):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--ai-level", level])


def test_elo_and_skill_cannot_both_be_given():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--elo", "1500", "--skill", "3"])
