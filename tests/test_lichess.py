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

from chessboard.drivers.board_input import BoardInput, Quit
from chessboard.drivers.led import ConsoleLEDDriver
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
    def __init__(self, events):
        self._events = events
        self.moves = []

    def stream_game(self, game_id):
        return iter(self._events)

    def make_move(self, game_id, uci):
        self.moves.append(uci)
        return {"ok": True}


class Scripted(BoardInput):
    def __init__(self, ucis):
        self._it = iter(ucis)

    def next_move(self, board):
        try:
            return chess.Move.from_uci(next(self._it))
        except StopIteration:
            raise Quit from None


def game_full(white="me", black="them", moves="", status="started"):
    return {
        "type": "gameFull",
        "white": {"id": white},
        "black": {"id": black},
        "state": {"type": "gameState", "moves": moves, "status": status},
    }


def test_adopts_colour_from_the_game_full_event():
    events = [game_full(white="me", black="them"),
              {"type": "gameState", "moves": "e2e4 e7e5", "status": "mate",
               "winner": "white"}]
    client = FakeClient(events)
    g = LichessGame(client, "abc", Scripted(["e2e4"]), QUIET, "me", echo=silent)
    g.run()
    assert g.my_color == chess.WHITE


def test_board_is_rebuilt_from_the_stream_not_applied_locally():
    """The move list is authoritative, so our own move is never double-applied."""
    events = [
        game_full(white="me", black="them"),
        {"type": "gameState", "moves": "e2e4 e7e5", "status": "started"},
        {"type": "gameState", "moves": "e2e4 e7e5 g1f3 b8c6", "status": "draw"},
    ]
    client = FakeClient(events)
    g = LichessGame(client, "abc", Scripted(["e2e4", "g1f3"]), QUIET, "me", echo=silent)
    g.run()
    assert g.game.san_history == ["e4", "e5", "Nf3", "Nc6"]
    assert g.game.ply == 4


def test_our_move_is_posted_only_on_our_turn():
    events = [
        game_full(white="me", black="them"),
        {"type": "gameState", "moves": "e2e4 e7e5", "status": "started"},
        {"type": "gameState", "moves": "e2e4 e7e5 g1f3", "status": "resign",
         "winner": "white"},
    ]
    client = FakeClient(events)
    g = LichessGame(client, "abc", Scripted(["e2e4", "g1f3"]), QUIET, "me", echo=silent)
    g.run()
    assert client.moves == ["e2e4", "g1f3"]   # not on Black's turns


def test_playing_black_waits_before_moving():
    events = [
        game_full(white="them", black="me"),
        {"type": "gameState", "moves": "e2e4", "status": "started"},
        {"type": "gameState", "moves": "e2e4 e7e5", "status": "stalemate"},
    ]
    client = FakeClient(events)
    g = LichessGame(client, "abc", Scripted(["e7e5"]), QUIET, "me", echo=silent)
    g.run()
    assert g.my_color == chess.BLACK
    assert client.moves == ["e7e5"]


def test_result_reports_which_side_you_were():
    # Opening state is Black to move, so the driver waits rather than prompting.
    events = [game_full(white="me", black="them", moves="e2e4"),
              {"type": "gameState", "moves": "e2e4 e7e5", "status": "mate",
               "winner": "black"}]
    g = LichessGame(FakeClient(events), "abc", Scripted([]), QUIET, "me", echo=silent)
    assert "you lost" in g.run()


def test_quitting_mid_game_aborts_cleanly():
    """Ctrl-D at the prompt must stop, not crash or post a move."""
    events = [game_full(white="me", black="them"),
              {"type": "gameState", "moves": "e2e4", "status": "started"}]
    client = FakeClient(events)
    g = LichessGame(client, "abc", Scripted([]), QUIET, "me", echo=silent)
    assert g.run() == "aborted"
    assert client.moves == []


def test_refuses_a_game_we_are_not_playing_in():
    events = [game_full(white="someone", black="other")]
    g = LichessGame(FakeClient(events), "abc", Scripted([]), QUIET, "me", echo=silent)
    with pytest.raises(LichessError, match="not a player"):
        g.run()


def test_chat_lines_do_not_disturb_the_game():
    seen = []
    events = [
        game_full(white="me", black="them"),
        {"type": "chatLine", "username": "them", "text": "hi"},
        {"type": "gameState", "moves": "e2e4", "status": "aborted"},
    ]
    g = LichessGame(FakeClient(events), "abc", Scripted(["e2e4"]), QUIET, "me",
                    echo=seen.append)
    g.run()
    assert any("hi" in line for line in seen)
