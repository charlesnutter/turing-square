"""Lichess client and game driver, tested without a network or a token.

The opener and the clock are injected, so the reconnect, keep-alive and
rate-limit paths -- the ones that only bite during a real game, hours in -- are
all exercised here deterministically.
"""

import io
import json
import urllib.error

import chess
import pytest

from chessboard.drivers.led import ConsoleLEDDriver
from chessboard.events import EOF, EventBus
from chessboard.lichess.client import (
    AuthError, Client, LichessError, RateLimited, load_token,
)
from chessboard.lichess.play import LichessGame

QUIET = ConsoleLEDDriver(echo=lambda _: None)
silent = lambda _: None


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, lines=(), body=""):
        self._lines = [l if isinstance(l, bytes) else l.encode() for l in lines]
        self._body = body.encode()

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body="{}"):
    return urllib.error.HTTPError(
        "https://lichess.org/x", code, "err", {}, io.BytesIO(body.encode())
    )


class FakeOpener:
    def __init__(self, responses):
        self.queue = list(responses)
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        if not self.queue:
            raise urllib.error.URLError("exhausted")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client_with(responses, **kw):
    sleeps = []
    c = Client("tok", opener=FakeOpener(responses),
               sleep=sleeps.append, **kw)
    return c, sleeps


# --------------------------------------------------------------------------
# token handling
# --------------------------------------------------------------------------

def test_token_comes_from_the_environment_first(monkeypatch, tmp_path):
    monkeypatch.setenv("LICHESS_TOKEN", "from-env")
    f = tmp_path / "t"
    f.write_text("from-file")
    assert load_token(f) == "from-env"


def test_token_falls_back_to_the_file(monkeypatch, tmp_path):
    monkeypatch.delenv("LICHESS_TOKEN", raising=False)
    f = tmp_path / "t"
    f.write_text("  from-file\n")
    assert load_token(f) == "from-file"


def test_missing_token_explains_how_to_make_one(monkeypatch, tmp_path):
    monkeypatch.delenv("LICHESS_TOKEN", raising=False)
    with pytest.raises(AuthError, match="board:play"):
        load_token(tmp_path / "absent")


# --------------------------------------------------------------------------
# HTTP error mapping
# --------------------------------------------------------------------------

def test_401_is_an_auth_error_naming_the_scope():
    c, _ = client_with([http_error(401)])
    with pytest.raises(AuthError, match="board:play"):
        c.account()


def test_429_is_its_own_error():
    c, _ = client_with([http_error(429)])
    with pytest.raises(RateLimited):
        c.account()


def test_other_http_errors_carry_the_status():
    c, _ = client_with([http_error(503, "upstream down")])
    with pytest.raises(LichessError, match="503"):
        c.account()


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------

def test_blank_keepalive_lines_are_skipped_not_parsed():
    """Lichess sends empty lines every few seconds; json.loads would choke."""
    resp = FakeResponse([b"\n", b'{"type":"gameState","moves":"e2e4"}\n', b"\n", b"\n"])
    c, _ = client_with([resp], max_reconnects=1)
    assert list(c.stream_game("abc")) == [{"type": "gameState", "moves": "e2e4"}]


def test_stream_reconnects_after_a_clean_close():
    """A long-lived stream ending is normal, not an error -- reconnect cheaply."""
    first = FakeResponse([b'{"n":1}\n'])
    second = FakeResponse([b'{"n":2}\n'])
    c, sleeps = client_with([first, second], max_reconnects=2)
    assert list(c.stream_events()) == [{"n": 1}, {"n": 2}]
    assert sleeps == [1.0, 1.0]          # flat, not exponential


def test_stream_backs_off_exponentially_on_failure():
    c, sleeps = client_with(
        [http_error(500), http_error(500), http_error(500)], max_reconnects=3
    )
    list(c.stream_events())
    assert sleeps == [1, 2, 4]


def test_backoff_is_capped():
    c, sleeps = client_with([http_error(500)] * 12, max_reconnects=12)
    list(c.stream_events())
    assert max(sleeps) == 60


def test_rate_limit_pauses_a_full_minute():
    c, sleeps = client_with([http_error(429)], max_reconnects=1)
    list(c.stream_events())
    assert sleeps == [60.0]


# --------------------------------------------------------------------------
# starting games
# --------------------------------------------------------------------------

@pytest.mark.parametrize("level", [0, 9, -1])
def test_ai_level_is_validated_before_the_request(level):
    c, _ = client_with([])
    with pytest.raises(ValueError, match="1-8"):
        c.challenge_ai(level)


def test_challenge_ai_posts_level_and_colour():
    c, _ = client_with([FakeResponse(body='{"id":"abcd1234"}')])
    assert c.challenge_ai(3, color="black")["id"] == "abcd1234"
    sent = c._opener.requests[0]
    assert sent.method == "POST"
    assert b"level=3" in sent.data and b"color=black" in sent.data


# --------------------------------------------------------------------------
# never touch the bot endpoints
# --------------------------------------------------------------------------

def test_no_bot_endpoints_are_ever_called():
    """Upgrading an account to BOT is irreversible and needs zero prior games.

    Matches path *literals* only, so the module docstring that explains this very
    rule does not trip its own guard.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "chessboard"
    literal = re.compile(r"""["']/api/bot""")
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if literal.search(text) or "account/upgrade" in text:
            offenders.append(path.name)
    assert offenders == []


# --------------------------------------------------------------------------
# the game driver
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, events=()):
        self._events = list(events)
        self.moves = []
        self.resigned = False

    def stream_game(self, game_id):
        return iter(self._events)

    def make_move(self, game_id, uci):
        self.moves.append(uci)
        return {"ok": True}

    def resign(self, game_id):
        self.resigned = True
        return {"ok": True}


def driver(client, typed=(), me="me"):
    return LichessGame(client, "abc", QUIET, me, lines=list(typed), echo=silent)


def play(client, events, typed=()):
    """Run one game with a deterministic event order, no threads."""
    game = driver(client, typed)
    bus = EventBus()
    for item in events:
        bus.post("lichess", item)
    bus.post("stdin", None, EOF)
    return game, game.run(bus=bus)


def interleaved(game, pairs):
    """Feed (source, payload) pairs in an exact order."""
    bus = EventBus()
    for source, payload in pairs:
        bus.post(source, payload)
    bus.post("stdin", None, EOF)
    return game.run(bus=bus)


def game_full(white="me", black="them", moves="", status="started",
              initial_fen="startpos"):
    return {
        "type": "gameFull",
        "white": {"id": white},
        "black": {"id": black},
        "initialFen": initial_fen,
        "state": {"type": "gameState", "moves": moves, "status": status},
    }


def test_adopts_colour_from_the_game_full_event():
    client = FakeClient()
    game = driver(client)
    interleaved(game, [
        ("lichess", game_full(white="me", black="them", moves="e2e4")),
        ("lichess", {"type": "gameState", "moves": "e2e4 e7e5", "status": "draw"}),
    ])
    assert game.my_color == chess.WHITE


def test_board_is_rebuilt_from_the_stream_not_applied_locally():
    """The move list is authoritative, so our own move is never double-applied."""
    client = FakeClient()
    game = driver(client, typed=[])
    interleaved(game, [
        ("lichess", game_full(white="me", black="them")),
        ("stdin", "e4"),
        ("lichess", {"type": "gameState", "moves": "e2e4 e7e5", "status": "started"}),
        ("stdin", "Nf3"),
        ("lichess", {"type": "gameState", "moves": "e2e4 e7e5 g1f3 b8c6",
                     "status": "draw"}),
    ])
    assert game.game.san_history == ["e4", "e5", "Nf3", "Nc6"]
    assert client.moves == ["e2e4", "g1f3"]
    assert game.game.ply == 4


def test_a_move_played_in_a_browser_reaches_the_terminal():
    """The point of the queue: nothing is typed, yet the board keeps advancing.

    Previously the loop blocked on input while it was our turn, so a move made
    elsewhere arrived at a socket nobody was reading.
    """
    client = FakeClient()
    game = driver(client)
    result = interleaved(game, [
        ("lichess", game_full(white="me", black="them")),          # our move
        ("lichess", {"type": "gameState", "moves": "e2e4 e7e5",    # played in a browser
                     "status": "started"}),
        ("lichess", {"type": "gameState", "moves": "e2e4 e7e5 g1f3 b8c6",
                     "status": "resign", "winner": "white"}),
    ])
    assert game.game.san_history == ["e4", "e5", "Nf3", "Nc6"]
    assert client.moves == []            # we never typed, so we never posted
    assert "you won" in result


def test_typing_out_of_turn_is_refused_without_posting():
    client = FakeClient()
    seen = []
    game = LichessGame(client, "abc", QUIET, "me", lines=[], echo=seen.append)
    interleaved(game, [
        ("lichess", game_full(white="me", black="them", moves="e2e4")),  # their move
        ("stdin", "d4"),
        ("lichess", {"type": "gameState", "moves": "e2e4 e7e5", "status": "draw"}),
    ])
    assert client.moves == []
    assert any("not your move" in line for line in seen)


def test_playing_black_waits_before_moving():
    client = FakeClient()
    game = driver(client)
    interleaved(game, [
        ("lichess", game_full(white="them", black="me")),
        ("lichess", {"type": "gameState", "moves": "e2e4", "status": "started"}),
        ("stdin", "e5"),
        ("lichess", {"type": "gameState", "moves": "e2e4 e7e5", "status": "stalemate"}),
    ])
    assert game.my_color == chess.BLACK
    assert client.moves == ["e7e5"]


def test_result_reports_which_side_you_were():
    game = driver(FakeClient())
    result = interleaved(game, [
        ("lichess", game_full(white="me", black="them", moves="e2e4")),
        ("lichess", {"type": "gameState", "moves": "e2e4 e7e5", "status": "mate",
                     "winner": "black"}),
    ])
    assert "you lost" in result


def test_refuses_a_game_we_are_not_playing_in():
    game = driver(FakeClient())
    with pytest.raises(LichessError, match="not a player"):
        interleaved(game, [("lichess", game_full(white="someone", black="other"))])


def test_chat_lines_do_not_disturb_the_game():
    seen = []
    game = LichessGame(FakeClient(), "abc", QUIET, "me", lines=[], echo=seen.append)
    interleaved(game, [
        ("lichess", game_full(white="me", black="them")),
        ("lichess", {"type": "chatLine", "username": "them", "text": "hi"}),
        ("lichess", {"type": "gameState", "moves": "e2e4", "status": "aborted"}),
    ])
    assert any("hi" in line for line in seen)


# --------------------------------------------------------------------------
# commands -- advertised in the help, and previously not wired at all
# --------------------------------------------------------------------------

def test_moves_and_fen_commands_produce_output():
    seen = []
    game = LichessGame(FakeClient(), "abc", QUIET, "me", lines=[], echo=seen.append)
    interleaved(game, [
        ("lichess", game_full(white="me", black="them")),
        ("stdin", "moves"),
        ("stdin", "fen"),
        ("lichess", {"type": "gameState", "moves": "", "status": "aborted"}),
    ])
    text = "\n".join(seen)
    assert "Nf3" in text                       # legal move list
    assert "rnbqkbnr" in text                  # fen


def test_resign_command_reaches_lichess():
    client = FakeClient()
    game = driver(client)
    interleaved(game, [
        ("lichess", game_full(white="me", black="them")),
        ("stdin", "resign"),
        ("lichess", {"type": "gameState", "moves": "", "status": "resign",
                     "winner": "black"}),
    ])
    assert client.resigned is True


def test_quit_leaves_the_game_running():
    client = FakeClient()
    game = driver(client)
    result = interleaved(game, [
        ("lichess", game_full(white="me", black="them")),
        ("stdin", "quit"),
    ])
    assert result == "aborted"
    assert client.resigned is False
    assert client.moves == []


def test_unreadable_input_does_not_end_the_game():
    client = FakeClient()
    game = driver(client)
    interleaved(game, [
        ("lichess", game_full(white="me", black="them")),
        ("stdin", "zzz"),
        ("stdin", "e4"),
        ("lichess", {"type": "gameState", "moves": "e2e4", "status": "aborted"}),
    ])
    assert client.moves == ["e2e4"]


# --------------------------------------------------------------------------
# special moves over the wire
#
# These arrive as plain UCI and go through the same rebuild path as everything
# else, so the risk is that python-chess reads them differently from how Lichess
# meant them. Castling in particular: Lichess sends `e1g1`, which is a two-square
# king move, not the Chess960 `e1h1` rook-capture spelling.
# --------------------------------------------------------------------------

SPECIALS = [
    ("kingside castling", "startpos",
     "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 e1g1", "O-O"),
    ("queenside castling", "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1",
     "e1c1", "O-O-O"),
    ("en passant", "startpos",
     "e2e4 a7a6 e4e5 d7d5 e5d6", "exd6"),
    ("promotion to queen", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1",
     "a7a8q", "a8=Q+"),
    ("underpromotion to knight", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1",
     "a7a8n", "a8=N"),
]


@pytest.mark.parametrize("label,fen,moves,last_san",
                         SPECIALS, ids=[s[0] for s in SPECIALS])
def test_special_moves_survive_the_rebuild(label, fen, moves, last_san):
    game = driver(FakeClient())
    interleaved(game, [
        ("lichess", game_full(white="me", black="them",
                              moves=moves, initial_fen=fen)),
        ("lichess", {"type": "gameState", "moves": moves, "status": "draw"}),
    ])
    assert game.game.san_history[-1] == last_san


def test_castling_actually_moves_the_rook():
    """O-O is two pieces. A king-only interpretation would pass a SAN check."""
    moves = "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 e1g1"
    game = driver(FakeClient())
    interleaved(game, [
        ("lichess", game_full(white="me", black="them", moves=moves)),
        ("lichess", {"type": "gameState", "moves": moves, "status": "draw"}),
    ])
    board = game.game.board
    assert board.piece_at(chess.G1) == chess.Piece(chess.KING, chess.WHITE)
    assert board.piece_at(chess.F1) == chess.Piece(chess.ROOK, chess.WHITE)
    assert board.piece_at(chess.H1) is None


def test_en_passant_removes_the_pawn_that_is_not_on_the_destination():
    moves = "e2e4 a7a6 e4e5 d7d5 e5d6"
    game = driver(FakeClient())
    interleaved(game, [
        ("lichess", game_full(white="me", black="them", moves=moves)),
        ("lichess", {"type": "gameState", "moves": moves, "status": "draw"}),
    ])
    board = game.game.board
    assert board.piece_at(chess.D6) == chess.Piece(chess.PAWN, chess.WHITE)
    assert board.piece_at(chess.D5) is None


@pytest.mark.parametrize("status,winner,expect", [
    ("mate", "white", "you won"),
    ("stalemate", None, "game over: stalemate"),
    ("outoftime", "black", "you lost"),
    ("draw", None, "game over: draw"),
    ("aborted", None, "game over: aborted"),
])
def test_every_terminal_status_ends_the_game(status, winner, expect):
    """Resign was verified live; these take the identical branch."""
    state = {"type": "gameState", "moves": "e2e4", "status": status}
    if winner:
        state["winner"] = winner
    game = driver(FakeClient())
    result = interleaved(game, [
        ("lichess", game_full(white="me", black="them", moves="e2e4")),
        ("lichess", state),
    ])
    assert expect in result
