"""The service between HTTP and the game loop.

The loop owns the board and consumes one queue; the service is what turns a
request into something on that queue and hands back what came of it. Nothing in
here reaches into the game directly -- that would be a second writer, which is
the thing the single queue exists to prevent.
"""

import threading

import chess
import pytest

from chessboard.api.service import GameService, ServiceError
from chessboard.modes import LOCAL_AI, LOCAL_HUMAN, ONLINE_HUMAN, GameRequest


class ToyEngine:
    """A local engine with no Stockfish behind it."""

    name = "toy"

    def __init__(self, ucis=()):
        self._ucis = list(ucis)
        self.closed = False

    def play(self, board):
        if self._ucis:
            return chess.Move.from_uci(self._ucis.pop(0))
        return sorted(board.legal_moves, key=lambda m: m.uci())[0]

    def describe(self):
        return "toy"

    def close(self):
        self.closed = True


@pytest.fixture
def service():
    """A service that never shells out to a real engine or prints anything."""
    made = []

    def open_engine(kind, **kwargs):
        engine = ToyEngine(["e7e5", "b8c6", "g8f6"])
        made.append(engine)
        return engine

    svc = GameService(open_engine=open_engine, echo=lambda _: None)
    svc.engines_made = made
    yield svc
    svc.stop()


def until(predicate, timeout=2.0):
    tick = threading.Event()
    for _ in range(int(timeout * 200)):
        if predicate():
            return True
        tick.wait(0.005)
    return False


# ---- starting -------------------------------------------------------------

def test_nothing_is_running_before_a_game_is_started(service):
    assert service.running is False
    with pytest.raises(ServiceError):
        service.state()


def test_starting_a_local_game_reports_the_opening_position(service):
    state = service.start(GameRequest(mode=LOCAL_HUMAN))
    assert service.running is True
    assert state["mode"] == LOCAL_HUMAN
    assert state["turn"] == "white"
    assert state["ply"] == 0
    assert state["players"] == {"white": None, "black": None}


def test_a_game_can_start_from_a_position(service):
    state = service.start(GameRequest(mode=LOCAL_HUMAN, fen="8/8/8/8/8/5k2/6q1/7K w - - 0 1"))
    assert state["fen"].startswith("8/8/8/8/8/5k2/6q1/7K")


def test_a_bad_fen_is_refused_rather_than_started(service):
    with pytest.raises(ServiceError, match="FEN"):
        service.start(GameRequest(mode=LOCAL_HUMAN, fen="not a position"))
    assert service.running is False


def test_options_that_imply_two_modes_are_refused(service):
    with pytest.raises(ServiceError, match="more than one mode"):
        service.start(GameRequest(elo=1500, lichess=True))


def test_starting_versus_an_engine_names_it_in_the_black_seat(service):
    state = service.start(GameRequest(mode=LOCAL_AI, skill=1))
    assert state["mode"] == LOCAL_AI
    assert state["players"]["black"] == "toy"
    assert state["players"]["white"] is None


def test_playing_black_puts_the_engine_in_the_white_seat(service):
    service.start(GameRequest(mode=LOCAL_AI, skill=1, black=True))
    assert service.state()["players"]["white"] == "toy"


def test_an_online_mode_is_refused_clearly_rather_than_half_wired(service):
    """Starting one creates a real game on the account, so it must not be a
    silent no-op or a half-working path."""
    with pytest.raises(ServiceError, match="not available"):
        service.start(GameRequest(mode=ONLINE_HUMAN))
    assert service.running is False


def test_starting_a_second_game_replaces_the_first(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    service.play("e4")
    state = service.start(GameRequest(mode=LOCAL_HUMAN))
    assert state["ply"] == 0, "the new game inherited the old one's moves"


# ---- playing --------------------------------------------------------------

def test_a_move_is_played_and_the_resulting_state_comes_back(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    state = service.play("e4")
    assert state["history"] == ["e4"]
    assert state["last_move"]["uci"] == "e2e4"
    assert state["turn"] == "black"


def test_uci_is_accepted_as_well_as_san(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    assert service.play("g1f3")["history"] == ["Nf3"]


def test_an_illegal_move_is_refused_with_a_reason_and_changes_nothing(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    with pytest.raises(ServiceError) as refused:
        service.play("e5")
    assert "e5" in str(refused.value) or "pawn" in str(refused.value)
    assert service.state()["ply"] == 0


def test_an_illegal_san_move_is_told_it_is_illegal_not_unreadable(service):
    """`Qd5` spells fine. The reason has to be the real one, or the player is
    sent looking for a typo that is not there."""
    service.start(GameRequest(mode=LOCAL_HUMAN))
    with pytest.raises(ServiceError) as refused:
        service.play("Qd5")
    assert "not a move I can read" not in str(refused.value)
    assert "not legal" in str(refused.value)


def test_text_that_is_not_a_move_at_all_is_refused(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    with pytest.raises(ServiceError):
        service.play("banana")


def test_a_command_cannot_be_smuggled_in_as_a_move(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    with pytest.raises(ServiceError):
        service.play("takeback")


def test_a_move_on_the_engine_s_turn_is_refused(service):
    """The engine is asked the moment White moves. Until it answers, the board
    is not ours to touch -- and the client is told so rather than ignored."""
    release = threading.Event()
    asked = threading.Event()

    class SlowEngine:
        name = "slow"

        def play(self, board):
            asked.set()
            release.wait(2.0)
            return chess.Move.from_uci("e7e5")

        def close(self):
            pass

    svc = GameService(open_engine=lambda kind, **kw: SlowEngine(),
                      echo=lambda _: None)
    try:
        svc.start(GameRequest(mode=LOCAL_AI, skill=1))
        svc.play("e4")
        assert asked.wait(2.0)
        with pytest.raises(ServiceError, match="engine"):
            svc.play("Nf3")
        assert svc.state()["thinking"] is True
        release.set()
        assert until(lambda: svc.state()["history"] == ["e4", "e5"])
        assert svc.state()["thinking"] is False
    finally:
        release.set()
        svc.stop()


def test_a_failed_start_leaves_the_running_game_alone(service):
    """Fat-fingering New Game must not end the game you are playing."""
    service.start(GameRequest(mode=LOCAL_HUMAN))
    service.play("e4")
    with pytest.raises(ServiceError):
        service.start(GameRequest(mode=LOCAL_HUMAN, fen="not a position"))
    assert service.running is True
    assert service.state()["history"] == ["e4"]


def test_the_engine_answers_without_being_asked_over_http(service):
    service.start(GameRequest(mode=LOCAL_AI, skill=1))
    service.play("e4")
    assert until(lambda: service.state()["history"] == ["e4", "e5"])


def test_a_move_cannot_be_played_before_a_game_exists(service):
    with pytest.raises(ServiceError):
        service.play("e4")


# ---- commands -------------------------------------------------------------

def test_takeback_returns_control_to_the_same_player(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    service.play("e4")
    service.play("e5")
    state = service.command("takeback")
    assert state["ply"] == 0


def test_an_unknown_command_is_refused(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    with pytest.raises(ServiceError, match="not a command"):
        service.command("launch")


def test_a_move_cannot_be_smuggled_in_as_a_command(service):
    service.start(GameRequest(mode=LOCAL_HUMAN))
    with pytest.raises(ServiceError):
        service.command("e4")


# ---- ending ---------------------------------------------------------------

def test_stopping_ends_the_game_and_closes_the_engine(service):
    service.start(GameRequest(mode=LOCAL_AI, skill=1))
    service.stop()
    assert service.running is False
    assert until(lambda: service.engines_made[0].closed)


def test_a_finished_game_stops_accepting_moves(service):
    service.start(GameRequest(
        mode=LOCAL_HUMAN,
        fen="rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 3"))
    state = service.play("Qh4#")
    assert state["over"] is True
    with pytest.raises(ServiceError):
        service.play("a3")


# ---- watchers -------------------------------------------------------------

def test_a_subscriber_is_pushed_the_state_after_every_change(service):
    pushed = []
    service.subscribe(pushed.append)
    service.start(GameRequest(mode=LOCAL_HUMAN))
    service.play("e4")
    assert until(lambda: any(s["history"] == ["e4"] for s in pushed))


def test_starting_a_game_is_pushed_to_watchers_who_were_already_connected(service):
    """A tablet open before you press New Game has no request to learn from --
    only the push tells it a game exists."""
    pushed = []
    service.subscribe(pushed.append)
    service.start(GameRequest(mode=LOCAL_HUMAN))
    assert until(lambda: any(s.get("running") and s["ply"] == 0 for s in pushed)), pushed


def test_stopping_a_game_is_pushed_too(service):
    """Otherwise every connected view keeps showing a game that has ended."""
    service.start(GameRequest(mode=LOCAL_HUMAN))
    pushed = []
    service.subscribe(pushed.append)
    service.stop()
    assert until(lambda: any(s.get("running") is False for s in pushed)), pushed


def test_restarting_does_not_announce_an_idle_moment_in_between(service):
    """New Game replaces a game; it does not end one. A client that saw
    running=false would flash 'no game' -- or conclude the game it was watching
    had finished."""
    service.start(GameRequest(mode=LOCAL_HUMAN))
    pushed = []
    service.subscribe(pushed.append)
    service.start(GameRequest(mode=LOCAL_HUMAN))
    assert until(lambda: any(s.get("running") for s in pushed))
    assert all(s.get("running") for s in pushed), pushed


def test_every_pushed_state_says_whether_a_game_is_running(service):
    """One shape for the client to read, whatever happened."""
    pushed = []
    service.subscribe(pushed.append)
    service.start(GameRequest(mode=LOCAL_HUMAN))
    service.play("e4")
    service.stop()
    assert until(lambda: len(pushed) >= 3)
    assert all("running" in state for state in pushed), pushed


def test_unsubscribing_stops_the_pushes(service):
    pushed = []
    cancel = service.subscribe(pushed.append)
    service.start(GameRequest(mode=LOCAL_HUMAN))
    cancel()
    service.play("e4")
    assert not any(s["history"] == ["e4"] for s in pushed)


def test_a_subscriber_that_raises_does_not_take_the_loop_down(service):
    """One dropped WebSocket must not stop the game for everyone else."""
    good = []

    def bad(_state):
        raise RuntimeError("this socket is gone")

    service.subscribe(bad)
    service.subscribe(good.append)
    service.start(GameRequest(mode=LOCAL_HUMAN))
    service.play("e4")
    assert until(lambda: any(s["history"] == ["e4"] for s in good))
