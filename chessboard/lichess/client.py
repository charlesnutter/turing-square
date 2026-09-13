"""Lichess Board API client, built on the standard library.

No HTTP dependency: `urllib.request` streams NDJSON perfectly well, and the
dependency policy is worth more here than the ergonomics of `requests`.

Three things about this API are easy to get wrong:

  * **The streams are long-lived NDJSON over HTTPS, not WebSockets.** They emit
    blank keep-alive lines every few seconds, and they *will* drop. Reconnecting
    with backoff is not optional.
  * **429 means stop for a full minute.** Lichess is explicit about it, and
    hammering the endpoint gets the token blocked.
  * **Only `/api/board/*` is used here, never `/api/bot/*`.** Upgrading an
    account to a BOT is irreversible and requires zero prior games. The Board API
    works with an ordinary account and a `board:play` token, so there is never a
    reason to touch the bot endpoints.
"""

import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterator, Optional

BASE_URL = "https://lichess.org"
DEFAULT_TOKEN_FILE = Path.home() / ".lichess-token"
TOKEN_ENV = "LICHESS_TOKEN"

MAX_BACKOFF = 60.0
RATE_LIMIT_PAUSE = 60.0


class LichessError(RuntimeError):
    """Any failure talking to Lichess."""


class AuthError(LichessError):
    """Token missing, malformed, or lacking the board:play scope."""


class RateLimited(LichessError):
    """HTTP 429. Back off for a full minute."""


def load_token(path: Optional[Path] = None, env: str = TOKEN_ENV) -> str:
    """Environment first, then a file. Never a literal in the repo.

    Warns if the file is readable by anyone but you -- a `board:play` token can
    play and resign your games.
    """
    token = os.environ.get(env, "").strip()
    if token:
        return token

    path = Path(path) if path else DEFAULT_TOKEN_FILE
    if not path.exists():
        raise AuthError(
            f"no token. Set ${env}, or put one in {path}.\n"
            f"Create it at https://lichess.org/account/oauth/token/create"
            f"?scopes[]=board:play&description=chessboard"
        )
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"warning: {path} is readable by others. chmod 600 {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise AuthError(f"{path} is empty")
    return token


class Client:
    """Talks to the Board API.

    `opener` and `sleep` are injected so the reconnect and rate-limit paths can
    be tested without a network or a wall clock.
    """

    def __init__(self, token: str, base_url: str = BASE_URL,
                 opener: Optional[Callable] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 max_reconnects: Optional[int] = None):
        if not token:
            raise AuthError("empty token")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._max_reconnects = max_reconnects

    # ---- plumbing ----------------------------------------------------------

    def _request(self, method: str, path: str, data: Optional[dict] = None,
                 accept: str = "application/json"):
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": accept,
                "User-Agent": "turing-square/0.1",
            },
        )
        try:
            return self._opener(req)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthError(
                    "401 from Lichess -- the token is wrong, revoked, or lacks "
                    "the board:play scope"
                ) from exc
            if exc.code == 429:
                raise RateLimited("429 from Lichess -- backing off") from exc
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise LichessError(f"HTTP {exc.code} on {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LichessError(f"could not reach Lichess: {exc.reason}") from exc

    def _json(self, method: str, path: str, data: Optional[dict] = None) -> dict:
        with self._request(method, path, data) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _stream(self, path: str) -> Iterator[dict]:
        """Yield NDJSON objects, reconnecting with backoff when the stream drops.

        Blank lines are Lichess's keep-alive and are skipped, not parsed.
        """
        attempt = 0
        reconnects = 0
        while True:
            try:
                with self._request("GET", path, accept="application/x-ndjson") as resp:
                    attempt = 0
                    for raw in resp:
                        line = raw.strip()
                        if not line:
                            continue
                        yield json.loads(line)
            except RateLimited:
                self._sleep(RATE_LIMIT_PAUSE)
            except LichessError:
                self._sleep(min(MAX_BACKOFF, 2 ** attempt))
                attempt += 1
            else:
                # Clean EOF: the server closed a long-lived stream. Reconnect,
                # but without the penalty backoff -- this is normal.
                self._sleep(1.0)

            reconnects += 1
            if self._max_reconnects is not None and reconnects >= self._max_reconnects:
                return

    # ---- account -----------------------------------------------------------

    def account(self) -> dict:
        return self._json("GET", "/api/account")

    def username(self) -> str:
        return self.account().get("username", "?")

    # ---- streams -----------------------------------------------------------

    def stream_events(self) -> Iterator[dict]:
        """gameStart, gameFinish, challenge, challengeCanceled."""
        return self._stream("/api/stream/event")

    def stream_game(self, game_id: str) -> Iterator[dict]:
        """gameFull once, then gameState on every move, plus chatLine."""
        return self._stream(f"/api/board/game/stream/{game_id}")

    # ---- moves -------------------------------------------------------------

    def make_move(self, game_id: str, uci: str) -> dict:
        return self._json("POST", f"/api/board/game/{game_id}/move/{uci}")

    def resign(self, game_id: str) -> dict:
        return self._json("POST", f"/api/board/game/{game_id}/resign")

    def abort(self, game_id: str) -> dict:
        return self._json("POST", f"/api/board/game/{game_id}/abort")

    # ---- starting a game ---------------------------------------------------

    def challenge_ai(self, level: int = 1, color: str = "white",
                     clock_limit: Optional[int] = None,
                     clock_increment: int = 0) -> dict:
        """Play Lichess's own AI. The quickest way to exercise this end to end.

        `level` is 1-8. Without a clock the game is correspondence-style and will
        not flag while you think.
        """
        if not 1 <= level <= 8:
            raise ValueError("Lichess AI level is 1-8")
        data = {"level": level, "color": color}
        if clock_limit is not None:
            data["clock.limit"] = clock_limit
            data["clock.increment"] = clock_increment
        return self._json("POST", "/api/challenge/ai", data)
