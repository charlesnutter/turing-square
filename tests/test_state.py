"""The state snapshot the client renders.

The tablet is a pure view: it sends commands and draws whatever it is pushed.
So this payload has to carry everything a board needs to be drawn correctly --
including the things a FEN alone does not say, like which move to highlight and
whether the game is over and why.
"""

import chess

from chessboard.api.state import snapshot
from chessboard.core.game import Game


def test_a_new_game_reports_white_to_move_at_ply_zero():
    state = snapshot(Game())
    assert state["turn"] == "white"
    assert state["ply"] == 0
    assert state["history"] == []
    assert state["over"] is False
    assert state["outcome"] is None


def test_the_fen_is_carried_verbatim():
    game = Game()
    assert snapshot(game)["fen"] == game.fen


def test_legal_moves_are_uci_so_a_board_can_validate_a_drag():
    state = snapshot(Game())
    assert len(state["legal_moves"]) == 20
    assert "e2e4" in state["legal_moves"]
    assert "e4" not in state["legal_moves"]        # SAN would be ambiguous here


def test_no_move_has_been_played_so_there_is_nothing_to_highlight():
    assert snapshot(Game())["last_move"] is None


def test_the_last_move_carries_both_squares_and_how_to_name_it():
    """The UI highlights two squares; the move list needs the SAN."""
    game = Game()
    game.play_text("e4")
    last = snapshot(game)["last_move"]
    assert last == {"from": "e2", "to": "e4", "uci": "e2e4", "san": "e4"}


def test_history_carries_what_a_client_needs_to_navigate():
    """Each entry is self-describing: what to print, what to highlight, and the
    position to draw if the player scrubs back to it."""
    game = Game()
    for text in ("e4", "e5", "Nf3"):
        game.play_text(text)
    state = snapshot(game)

    assert [entry["san"] for entry in state["history"]] == ["e4", "e5", "Nf3"]
    assert state["history"][0]["uci"] == "e2e4"
    assert chess.Board(state["history"][0]["fen"]).turn == chess.BLACK
    assert state["history"][-1]["fen"] == game.fen
    assert state["movetext"] == "1. e4 e5 2. Nf3"
    assert state["ply"] == 3


def test_the_starting_position_is_carried_so_ply_zero_can_be_drawn():
    game = Game()
    game.play_text("e4")
    assert snapshot(game)["start_fen"] == chess.STARTING_FEN


def test_takeback_is_offered_when_there_is_something_to_take_back():
    """The client greys the button from this rather than firing a request that
    comes back 400."""
    game = Game()
    assert snapshot(game)["can"]["takeback"] is False
    game.play_text("e4")
    assert snapshot(game)["can"]["takeback"] is True


def test_takeback_is_not_offered_once_the_game_is_over():
    game = Game("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 3")
    game.play_text("Qh4#")
    assert snapshot(game)["can"]["takeback"] is False


def test_takeback_can_be_refused_by_the_caller():
    """Lichess games have no takeback here, and the client must be told so
    rather than working it out from the mode string."""
    game = Game()
    game.play_text("e4")
    assert snapshot(game, takeback=False)["can"]["takeback"] is False


def test_check_is_reported_so_the_board_can_mark_the_king():
    game = Game("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 3")
    game.play_text("Qh4#")
    assert snapshot(game)["check"] is True


def test_checkmate_ends_the_game_and_says_who_won():
    game = Game("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 3")
    game.play_text("Qh4#")
    state = snapshot(game)
    assert state["over"] is True
    assert "Black wins" in state["outcome"]
    assert state["legal_moves"] == []


def test_a_draw_says_how_it_was_drawn():
    game = Game("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")      # stalemate
    state = snapshot(game)
    assert state["over"] is True
    assert "draw" in state["outcome"]


def test_the_mode_and_who_holds_each_seat_are_reported():
    """The UI says 'you are White against Stockfish 19' from this."""
    state = snapshot(Game(), mode="local-ai", engines={chess.BLACK: "Stockfish 19"})
    assert state["mode"] == "local-ai"
    assert state["players"] == {"white": None, "black": "Stockfish 19"}


def test_a_local_two_player_game_has_no_engine_in_either_seat():
    state = snapshot(Game(), mode="local-human")
    assert state["players"] == {"white": None, "black": None}


def test_thinking_is_reported_so_the_client_can_disable_input():
    """A move typed on the engine's turn is refused, so the UI must know."""
    assert snapshot(Game())["thinking"] is False
    assert snapshot(Game(), thinking=True)["thinking"] is True


def test_the_snapshot_is_json_serialisable():
    """It goes over a WebSocket. A chess.Move in there would break the push."""
    import json
    game = Game()
    game.play_text("e4")
    json.dumps(snapshot(game, mode="local-ai", engines={chess.BLACK: "toy"}))


def test_history_does_not_bloat_the_push():
    """Every state change pushes the whole thing, so the per-ply cost matters."""
    import json
    game = Game()
    for uci in [m.uci() for m in list(game.legal_moves())[:1]]:
        game.play_text(uci)
    one_ply = len(json.dumps(snapshot(game)))
    assert one_ply < 2000, one_ply
