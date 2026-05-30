from __future__ import annotations

import logging
from typing import Optional

import requests
from requests import Session

from Field import Field
from data_structures import Direction, ItemKind

logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """Non-200 response from the server (carries the HTTP status code)."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {body[:120]}")
        self.status = status


class SnakeFieldAPI:
    def __init__(
        self,
        base_url: str,
        teamname: str,
        game_name: str,
        password: str,
        *,
        timeout: float = 0.5,
        session: Optional[Session] = None,
    ) -> None:
        self.team_name = teamname
        self.game_name = game_name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.auth = (teamname, password)
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get_field(self) -> Field:
        url = self._url(f"/games/{self.game_name}/state")
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise ApiError(resp.status_code, resp.text)
        return Field.from_dict(resp.json())

    def set_direction(self, direction: Direction) -> int:
        url = self._url(f"/games/{self.game_name}/snake/direction")
        resp = self.session.post(url, json={"direction": direction}, timeout=self.timeout)
        return resp.status_code

    def activate_item(self, item: ItemKind) -> int:
        url = self._url(f"/games/{self.game_name}/snake/activate")
        resp = self.session.post(url, json={"item": item}, timeout=self.timeout)
        return resp.status_code

    def reset_game(self) -> int:
        """Reset the game to a fresh round (the GUI's reset button). After a
        round ends the game refuses joins until reset, so this is how we restart
        test games without touching the web UI."""
        url = self._url(f"/games/{self.game_name}/reset")
        resp = self.session.post(url, timeout=self.timeout)
        return resp.status_code
