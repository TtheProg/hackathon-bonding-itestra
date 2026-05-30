"""Ouroboros snake bot.

Loop per tick:
  1. GET the field.
  2. Verify last tick's command actually landed (observed head move vs commanded)
     and calibrate the NORTH/SOUTH/EAST/WEST -> (dx, dy) mapping accordingly.
  3. Run an anytime iterative-deepening paranoid minimax.

A background "poster" thread re-POSTs the current best direction while the
search keeps deepening, so the server always holds our latest best move when
the tick fires. The server rate-limits aggressively (HTTP 429 on bursts), so
posts are throttled; only the last post before a tick counts.

Logging: every tick logs the board, all snakes, the decision (move/depth/score),
the POST status, and whether last tick's command actually moved our head. Logs
go to the console and to ../logs/ouroboros-<timestamp>.log.
"""
import argparse
import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from api import ApiError, SnakeFieldAPI
from data_structures import Direction
from engine import (
    DEFAULT_DELTAS,
    SearchResult,
    choose_direction,
    observed_delta,
    render_board,
    state_from_field,
)

log = logging.getLogger("ouroboros")

TICK_SECONDS = 1.0
# The server token-buckets requests; >~3/s triggers 429. We do 1 GET/tick plus
# throttled posts. Only the most recent post before the tick is used.
POST_INTERVAL = 0.45
SEARCH_BUDGET = 0.80


def setup_logging(verbose: bool) -> str:
    logs_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, f"ouroboros-{datetime.now():%Y%m%d-%H%M%S}.log")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")

    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(console)
    fileh = logging.FileHandler(path)
    fileh.setFormatter(fmt)
    fileh.setLevel(logging.DEBUG)  # file always keeps the full detail
    log.addHandler(fileh)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return path


class BestMove:
    """Thread-shared latest-best direction."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._direction: Optional[Direction] = None
        self.generation = 0

    def set(self, direction: Direction) -> None:
        with self._lock:
            self._direction = direction
            self.generation += 1

    def get(self) -> Tuple[Optional[Direction], int]:
        with self._lock:
            return self._direction, self.generation


class Poster(threading.Thread):
    """Posts the current best direction, throttled to POST_INTERVAL.

    Tracks status codes so the main loop can confirm posts are landing.
    """

    def __init__(self, api: SnakeFieldAPI, best: BestMove) -> None:
        super().__init__(daemon=True)
        self.api = api
        self.best = best
        self._stop = threading.Event()
        self._last_gen = -1
        # observable stats
        self.last_status: Optional[int] = None
        self.last_direction: Optional[Direction] = None
        self.posts_total = 0
        self.posts_ok = 0
        self.posts_429 = 0

    def run(self) -> None:
        while not self._stop.is_set():
            direction, gen = self.best.get()
            if direction is not None and gen != self._last_gen:
                try:
                    code = self.api.set_direction(direction)
                    self.posts_total += 1
                    self.last_status = code
                    self.last_direction = direction
                    if code == 200:
                        self.posts_ok += 1
                        self._last_gen = gen
                    elif code == 429:
                        self.posts_429 += 1
                    else:
                        log.debug("POST %s -> HTTP %s", direction, code)
                    log.debug("POST %s -> %s (gen %d)", direction, code, gen)
                except Exception as exc:
                    log.debug("POST %s failed: %s", direction, exc)
            self._stop.wait(POST_INTERVAL)

    def stop(self) -> None:
        self._stop.set()


def state_digest(state) -> str:
    parts = []
    for name, s in state.snakes.items():
        tag = "ME" if name == state.me else name[:8]
        alive = "" if s.alive else "†"
        parts.append(f"{tag}{alive}@{s.head}l{s.length}")
    apples = sorted(state.apples)
    return f"snakes[{', '.join(parts)}] apples{apples}"


def try_reset(api: SnakeFieldAPI, reason: str) -> None:
    try:
        code = api.reset_game()
        log.info("RESET game '%s' (%s) -> HTTP %s", api.game_name, reason, code)
    except Exception as exc:
        log.warning("reset failed: %s", exc)
    time.sleep(0.8)


def run(api: SnakeFieldAPI, team: str, opp_k: int, auto_reset: bool) -> None:
    best = BestMove()
    poster = Poster(api, best)
    poster.start()

    deltas = dict(DEFAULT_DELTAS)
    last_direction: Optional[Direction] = None
    prev_head: Optional[Tuple[int, int]] = None
    tick_no = 0
    not_appearing = 0

    try:
        while True:
            cycle_start = time.monotonic()
            try:
                field = api.get_field()
            except ApiError as exc:
                if exc.status == 429:
                    time.sleep(0.3)
                else:
                    log.debug("GET state -> %s", exc)
                    time.sleep(0.4)
                continue
            except Exception as exc:
                log.warning("GET state failed: %s", exc)
                time.sleep(0.4)
                continue

            state = state_from_field(field, team)
            if state is None:
                # Keep (re)posting so registration retries until we appear.
                best.set("NORTH")
                not_appearing += 1
                log.info("waiting to appear in game '%s' (snakes: %s, try %d)...",
                         api.game_name, list(field.snakes.keys()), not_appearing)
                # A post-game/stuck game refuses joins; auto-reset to recover.
                if auto_reset and not_appearing >= 5:
                    try_reset(api, "could not join")
                    not_appearing = 0
                time.sleep(0.6)
                continue
            not_appearing = 0

            me = state.snakes[team]
            if not me.alive:
                alive_others = len(state.alive_names())
                if auto_reset and alive_others <= 1:
                    # Round is effectively over -> restart for the next test run.
                    log.info("snake DEAD (len %d), round over. auto-resetting.",
                             me.length)
                    last_direction, prev_head = None, None
                    try_reset(api, "round over")
                    continue
                log.info("snake DEAD (final length %d). standing by for reset.",
                         me.length)
                last_direction, prev_head = None, None
                time.sleep(1.0)
                continue

            # --- did last tick's command actually land? ---
            landed_note = "first-move"
            if last_direction and prev_head is not None:
                if me.head == prev_head:
                    landed_note = f"NO-TICK (head still {me.head}; GET outran server tick)"
                else:
                    obs = observed_delta(prev_head, me.head, state.size)
                    expected = deltas[last_direction]
                    if obs == expected:
                        landed_note = f"OK cmd={last_direction} moved={obs}"
                    elif obs in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        # head moved, but not how we expected -> fix the mapping
                        if deltas[last_direction] != obs:
                            deltas = dict(deltas)
                            deltas[last_direction] = obs
                            log.info("CALIBRATED %s -> %s", last_direction, obs)
                        landed_note = f"REMAPPED cmd={last_direction} moved={obs}"
            if me.head != prev_head or prev_head is None:
                tick_no += 1
            prev_head = me.head

            # --- decide ---
            posts_before = poster.posts_total
            deadline = cycle_start + SEARCH_BUDGET
            result: SearchResult = choose_direction(
                state, deadline, deltas, opp_k=opp_k, on_improve=best.set
            )
            last_direction = result.direction

            # --- log everything ---
            log.info("=== tick~%d | %s", tick_no, state_digest(state))
            log.info("board:\n%s", render_board(state))
            log.info(
                "DECIDE move=%s depth=%d score=%.0f | last-move: %s",
                result.direction, result.depth, result.score, landed_note,
            )
            log.info(
                "POST status: last=%s dir=%s | totals ok=%d 429=%d all=%d (+%d this tick)",
                poster.last_status, poster.last_direction, poster.posts_ok,
                poster.posts_429, poster.posts_total,
                poster.posts_total - posts_before,
            )

            elapsed = time.monotonic() - cycle_start
            if elapsed < TICK_SECONDS:
                time.sleep(TICK_SECONDS - elapsed)
    finally:
        poster.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ouroboros snake bot")
    parser.add_argument("--team_name", default="Ouroboros", help="Team/snake name")
    parser.add_argument("--game_name", default="Ouroboros", help="Game to join")
    parser.add_argument("--password", default="hermeticism", help="Server password")
    parser.add_argument(
        "--base_url", default="http://192.168.7.211:3030", help="Game server base URL"
    )
    parser.add_argument(
        "--opp_k", type=int, default=2,
        help="How many nearest opponents to model in minimax (branching cost)",
    )
    parser.add_argument("--reset", action="store_true",
                        help="Reset the game once before joining")
    parser.add_argument("--auto-reset", action="store_true",
                        help="Auto-reset for hands-free testing: restart the "
                             "round whenever we die/the game ends or we can't join")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG logging on console (per-POST detail)")
    args = parser.parse_args()

    logpath = setup_logging(args.verbose)
    api = SnakeFieldAPI(
        args.base_url, args.team_name, args.game_name, args.password, timeout=0.5
    )
    log.info("Ouroboros -> %s game=%s as %s | reset=%s auto_reset=%s | logfile=%s",
             args.base_url, args.game_name, args.team_name,
             args.reset, args.auto_reset, logpath)
    if args.reset or args.auto_reset:
        try_reset(api, "startup")
    try:
        run(api, args.team_name, args.opp_k, args.auto_reset)
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
