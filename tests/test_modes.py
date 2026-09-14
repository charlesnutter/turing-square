"""Mode resolution, independent of argparse.

The API and the CLI must agree on what starts a game, so both build the same
request object and hand it to the same resolver. These tests build one directly
-- no parser involved -- which is the whole point of the type existing.
"""

import pytest

from chessboard.modes import (
    LOCAL_AI, LOCAL_HUMAN, ONLINE_AI, ONLINE_HUMAN, GameRequest, resolve_mode,
)


def test_a_request_built_without_a_parser_resolves_a_mode():
    assert resolve_mode(GameRequest(mode=LOCAL_AI, elo=1500)) == (LOCAL_AI, None)


def test_an_empty_request_is_two_players():
    assert resolve_mode(GameRequest()) == (LOCAL_HUMAN, None)


def test_strength_alone_implies_the_local_engine():
    assert resolve_mode(GameRequest(skill=3)) == (LOCAL_AI, None)


def test_a_game_id_alone_implies_playing_a_person():
    assert resolve_mode(GameRequest(game_id="abcd1234")) == (ONLINE_HUMAN, None)


def test_an_ai_level_alone_implies_the_lichess_engine():
    assert resolve_mode(GameRequest(ai_level=4)) == (ONLINE_AI, None)


def test_contradictory_fields_are_refused_rather_than_guessed():
    mode, error = resolve_mode(GameRequest(elo=1500, lichess=True))
    assert mode is None
    assert "more than one mode" in error


def test_the_request_carries_the_options_a_runner_needs():
    """The API sends JSON; the runner must not have to reach for a Namespace."""
    request = GameRequest(mode=LOCAL_AI, engine="stockfish", skill=5,
                          depth=8, threads=2, black=True)
    assert (request.engine, request.skill, request.depth) == ("stockfish", 5, 8)
    assert request.threads == 2 and request.black is True
