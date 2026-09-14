"""The HTTP surface.

The Pi is the single source of truth: these routes carry commands in and push
state out, and the client renders whatever it is told. Nothing here decides
anything about chess -- every route is a thin translation of a service call
into a status code.
"""

import threading

import chess
import pytest
from fastapi.testclient import TestClient

from chessboard.api.app import create_app
from chessboard.api.service import GameService
from chessboard.drivers.led import ConsoleLEDDriver


class ToyEngine:
    name = "toy"

    def __init__(self):
        self._ucis = ["e7e5", "b8c6"]

    def play(self, board):
        if self._ucis:
            return chess.Move.from_uci(self._ucis.pop(0))
        return sorted(board.legal_moves, key=lambda m: m.uci())[0]

    def close(self):
        pass


@pytest.fixture
def client():
    service = GameService(open_engine=lambda kind, **kw: ToyEngine(),
                          leds=ConsoleLEDDriver(echo=lambda _: None),
                          echo=lambda _: None)
    with TestClient(create_app(service)) as test_client:
        yield test_client
    service.stop()


def sans(state):
    """The move list as plain SAN -- history entries also carry uci and fen."""
    return [entry["san"] for entry in state.get("history", [])]


def pushed_until(socket, predicate, limit=6, timeout=5.0):
    """Read pushed states until one matches, without ever hanging the suite.

    `receive_json` blocks with no timeout, so a regression that pushes one
    message too few would hang pytest rather than fail it. Read on a daemon
    thread and give up instead.
    """
    seen = []

    def reader():
        for _ in range(limit):
            seen.append(socket.receive_json())
            if predicate(seen[-1]):
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    return seen


# ---- state ----------------------------------------------------------------

def test_asking_for_state_before_a_game_is_a_normal_answer_not_an_error(client):
    """Opening the tablet before starting a game is not a failure."""
    body = client.get("/api/state").json()
    assert client.get("/api/state").status_code == 200
    assert body["running"] is False


def test_state_after_starting_describes_the_board(client):
    client.post("/api/game", json={"mode": "local-human"})
    body = client.get("/api/state").json()
    assert body["running"] is True
    assert body["turn"] == "white"
    assert body["ply"] == 0
    assert "e2e4" in body["legal_moves"]


# ---- starting -------------------------------------------------------------

def test_starting_a_game_returns_the_opening_state(client):
    response = client.post("/api/game", json={"mode": "local-human"})
    assert response.status_code == 200
    assert response.json()["mode"] == "local-human"


def test_starting_against_an_engine_names_it(client):
    response = client.post("/api/game", json={"mode": "local-ai", "skill": 1})
    assert response.json()["players"]["black"] == "toy"


def test_a_bad_fen_is_a_client_error(client):
    response = client.post("/api/game", json={"mode": "local-human", "fen": "nope"})
    assert response.status_code == 400
    assert "FEN" in response.json()["detail"]


def test_an_online_mode_says_it_is_not_implemented_here(client):
    response = client.post("/api/game", json={"mode": "online-human"})
    assert response.status_code == 501


def test_an_unknown_mode_is_rejected_by_validation(client):
    assert client.post("/api/game", json={"mode": "chutes-and-ladders"}).status_code == 422


def test_an_empty_body_starts_two_player(client):
    """The default is the mode that needs nothing configured."""
    assert client.post("/api/game", json={}).json()["mode"] == "local-human"


# ---- moving ---------------------------------------------------------------

def test_a_legal_move_is_played(client):
    client.post("/api/game", json={"mode": "local-human"})
    body = client.post("/api/move", json={"move": "e4"}).json()
    assert sans(body) == ["e4"]
    assert body["last_move"] == {"from": "e2", "to": "e4", "uci": "e2e4", "san": "e4"}


def test_an_illegal_move_comes_back_with_the_reason(client):
    client.post("/api/game", json={"mode": "local-human"})
    response = client.post("/api/move", json={"move": "e5"})
    assert response.status_code == 400
    assert response.json()["detail"]
    assert client.get("/api/state").json()["ply"] == 0


def test_moving_before_a_game_exists_is_a_conflict(client):
    assert client.post("/api/move", json={"move": "e4"}).status_code == 409


def test_a_move_must_actually_be_supplied(client):
    client.post("/api/game", json={"mode": "local-human"})
    assert client.post("/api/move", json={}).status_code == 422


# ---- commands -------------------------------------------------------------

def test_a_command_is_carried_out(client):
    client.post("/api/game", json={"mode": "local-human"})
    client.post("/api/move", json={"move": "e4"})
    client.post("/api/move", json={"move": "e5"})
    # Two people, so takeback undoes one move, not both.
    assert client.post("/api/command", json={"command": "takeback"}).json()["ply"] == 1


def test_an_unknown_command_is_refused(client):
    client.post("/api/game", json={"mode": "local-human"})
    assert client.post("/api/command", json={"command": "launch"}).status_code == 400


def test_quit_is_not_a_command_a_stray_request_can_send(client):
    """Ending the game is DELETE /api/game, not a command anyone can post."""
    client.post("/api/game", json={"mode": "local-human"})
    assert client.post("/api/command", json={"command": "quit"}).status_code == 400
    assert client.get("/api/state").json()["running"] is True


# ---- ending ---------------------------------------------------------------

def test_deleting_the_game_stops_it(client):
    client.post("/api/game", json={"mode": "local-human"})
    assert client.delete("/api/game").status_code == 200
    assert client.get("/api/state").json()["running"] is False


# ---- the state push -------------------------------------------------------

def test_a_socket_is_sent_the_current_state_as_soon_as_it_connects(client):
    client.post("/api/game", json={"mode": "local-human"})
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["ply"] == 0


def test_a_socket_connecting_before_any_game_is_told_nothing_is_running(client):
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["running"] is False


def test_a_socket_open_before_the_game_starts_is_told_when_it_does(client):
    """The tablet is usually open first. It must not sit on 'nothing running'
    until somebody happens to move."""
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["running"] is False
        client.post("/api/game", json={"mode": "local-human"})
        seen = pushed_until(socket, lambda s: s["running"] is True)
    assert any(s["running"] and s["ply"] == 0 for s in seen), seen


def test_a_socket_is_told_when_the_game_is_deleted(client):
    client.post("/api/game", json={"mode": "local-human"})
    with client.websocket_connect("/ws") as socket:
        socket.receive_json()
        client.delete("/api/game")
        seen = pushed_until(socket, lambda s: s["running"] is False)
    assert any(s["running"] is False for s in seen), seen


def test_a_move_made_over_http_is_pushed_to_the_socket(client):
    """This is the whole point: the board on the tablet updates without asking."""
    client.post("/api/game", json={"mode": "local-human"})
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["ply"] == 0           # the state on connect
        client.post("/api/move", json={"move": "e4"})
        seen = pushed_until(socket, lambda s: sans(s) == ["e4"])
    assert any(sans(s) == ["e4"] and s["turn"] == "black" for s in seen), seen


def test_an_engine_s_own_move_is_pushed_without_anyone_asking(client):
    """Nobody sends a request when the engine moves, so only the push carries it."""
    client.post("/api/game", json={"mode": "local-ai", "skill": 1})
    with client.websocket_connect("/ws") as socket:
        socket.receive_json()
        client.post("/api/move", json={"move": "e4"})
        seen = pushed_until(socket, lambda s: sans(s) == ["e4", "e5"])
    assert any(sans(s) == ["e4", "e5"] for s in seen), seen


def test_two_sockets_both_see_the_move(client):
    """The board and a second tablet must not diverge."""
    client.post("/api/game", json={"mode": "local-human"})
    with client.websocket_connect("/ws") as first, \
            client.websocket_connect("/ws") as second:
        first.receive_json()
        second.receive_json()
        client.post("/api/move", json={"move": "d4"})
        on_first = pushed_until(first, lambda s: sans(s) == ["d4"])
        on_second = pushed_until(second, lambda s: sans(s) == ["d4"])
    assert any(sans(s) == ["d4"] for s in on_first), on_first
    assert any(sans(s) == ["d4"] for s in on_second), on_second
