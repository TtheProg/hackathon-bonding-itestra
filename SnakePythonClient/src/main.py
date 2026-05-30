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
import contextlib
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
# Hard target: finish ALL per-tick work (GET + search + logging) within half the
# tick. Running close to the full 1s is fatal -- one slow GET and we miss the
# server tick entirely, so we keep a large safety margin.
CYCLE_BUDGET = 0.50
# Anytime search deadline, measured from the TOP of the tick (so a slow GET eats
# into it rather than adding on top). Kept a hair under CYCLE_BUDGET to leave
# room for the logging block; total work lands at ~SEARCH_BUDGET + a few ms.
SEARCH_BUDGET = 0.45
# A GET this slow has already eaten most of the search budget -- surface it.
GET_WARN_MS = 150.0
# The server token-buckets requests; >~3/s triggers 429. We do 1 GET/tick plus
# throttled posts. Only the most recent post before the tick is used.
POST_INTERVAL = 0.45


@contextlib.contextmanager
def timed(label: str, sink: Optional[Dict[str, float]] = None,
          warn_ms: Optional[float] = None):
    """Measure the wall time of a costly block in milliseconds.

    Records into `sink[label]` (so the tick summary can report it) and logs the
    timing at DEBUG. If `warn_ms` is given and exceeded, logs at WARNING instead,
    so a slow operation surfaces on the console even without -v.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        ms = (time.monotonic() - start) * 1000.0
        if sink is not None:
            sink[label] = ms
        if warn_ms is not None and ms > warn_ms:
            log.warning("SLOW %s took %.1f ms (> %.0f ms)", label, ms, warn_ms)
        else:
            log.debug("timing %s = %.1f ms", label, ms)


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


_KNOWN_RAW_KEYS = {"snake", "snakes", "size", "items"}


def raw_extras(api: SnakeFieldAPI) -> str:
    """Top-level fields in the raw /state payload that Field drops (e.g. a
    server-side tick or score), so they show up in the logs."""
    raw = getattr(api, "last_raw", None)
    if not isinstance(raw, dict):
        return ""
    extras = {k: v for k, v in raw.items() if k not in _KNOWN_RAW_KEYS}
    return f" | raw{extras}" if extras else ""


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


def try_recreate(api: SnakeFieldAPI, reason: str) -> None:
    """Last-resort restart: delete and recreate the game with a known-good
    auto-start config, so a stuck game can't block development."""
    try:
        dcode = api.delete_game()
        ccode = api.create_game()
        log.info("RECREATE game '%s' (%s) -> delete=%s create=%s",
                 api.game_name, reason, dcode, ccode)
    except Exception as exc:
        log.warning("recreate failed: %s", exc)
    time.sleep(1.0)


def run(api: SnakeFieldAPI, team: str, opp_k: int, auto_reset: bool) -> None:
    best = BestMove()
    poster = Poster(api, best)
    poster.start()

    deltas = dict(DEFAULT_DELTAS)
    last_direction: Optional[Direction] = None
    prev_head: Optional[Tuple[int, int]] = None
    tick_no = 0
    not_appearing = 0
    # Start of the previous *decision* tick, to measure the real loop period
    # (how long between two moves -- should track the server's 1s tick).
    prev_tick_start: Optional[float] = None

    try:
        while True:
            cycle_start = time.monotonic()
            period_ms = (
                (cycle_start - prev_tick_start) * 1000.0
                if prev_tick_start is not None else None
            )
            timings: Dict[str, float] = {}
            try:
                with timed("GET state", timings, warn_ms=GET_WARN_MS):
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
                # Escalate recovery: a stuck/post-game state refuses joins.
                if auto_reset:
                    if not_appearing == 5:
                        try_reset(api, "could not join")
                    elif not_appearing >= 10:
                        try_recreate(api, "join still failing after reset")
                        not_appearing = 0
                time.sleep(0.6)
                continue
            not_appearing = 0

            me = state.snakes[team]
            if not me.alive:
                # Log the final standings ("game end and state") then, if
                # auto-resetting, restart immediately so we don't sit in a game
                # that may otherwise run forever.
                standings = sorted(
                    state.snakes.items(), key=lambda kv: kv[1].length, reverse=True
                )
                board = ", ".join(
                    f"{'*' if n == team else ''}{n}={s.length}"
                    f"{'(alive)' if s.alive else '†'}"
                    for n, s in standings
                )
                place = [n for n, _ in standings].index(team) + 1
                log.info("GAME END | our snake DEAD len=%d | place %d/%d | %s",
                         me.length, place, len(standings), board)
                log.info("final board:\n%s", render_board(state))
                last_direction, prev_head = None, None
                if auto_reset:
                    try_reset(api, "our snake died")
                    best.set("NORTH")  # rejoin immediately -> auto-starts round
                else:
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
                    else:
                        # Mapping is confirmed correct (verified via the API steer
                        # test), so a mismatch is the one-tick command lag -- our
                        # POST landed after the server locked that tick's move, so
                        # the snake kept its prior heading. NOT a mapping error;
                        # do not mutate the deltas (that caused calibration churn).
                        landed_note = (f"LAG cmd={last_direction} "
                                       f"expected={expected} moved={obs}")
            if me.head != prev_head or prev_head is None:
                tick_no += 1
            prev_head = me.head

            # --- decide ---
            posts_before = poster.posts_total
            deadline = cycle_start + SEARCH_BUDGET
            with timed("search", timings):
                result: SearchResult = choose_direction(
                    state, deadline, deltas, opp_k=opp_k, on_improve=best.set
                )
            last_direction = result.direction

            # --- log everything ---
            with timed("logging", timings):
                log.info("=== tick~%d | %s%s", tick_no, state_digest(state),
                         raw_extras(api))
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

            # --- timing summary: did we stay inside the half-tick budget? ---
            elapsed = time.monotonic() - cycle_start
            work_ms = elapsed * 1000.0
            tick_ms = TICK_SECONDS * 1000.0
            cap_ms = CYCLE_BUDGET * 1000.0
            if work_ms > tick_ms:
                status = "MISSED-TICK"   # fatal: ran past the server's tick
            elif work_ms > cap_ms:
                status = "OVER-BUDGET"   # past our 0.5s half-tick target
            else:
                status = "OK"
            period_str = f" period={period_ms:.0f}ms" if period_ms is not None else ""
            timing_line = (
                "TIMING %s work=%.0fms (get=%.0f search=%.0f/d%d/n%d log=%.0f)"
                "%s cap=%.0fms tick=%.0fms"
                % (status, work_ms, timings.get("GET state", 0.0),
                   timings.get("search", 0.0), result.depth, result.nodes,
                   timings.get("logging", 0.0), period_str, cap_ms, tick_ms)
            )
            (log.info if status == "OK" else log.warning)(timing_line)

            prev_tick_start = cycle_start
            if elapsed < TICK_SECONDS:
                time.sleep(TICK_SECONDS - elapsed)
    finally:
        poster.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ouroboros snake bot")
    parser.add_argument("--team_name", default="Ouroboros", help="Team/snake name")
    parser.add_argument("--game_name", default="braveFalcon", help="Game to join")
    parser.add_argument("--password", default="test", help="Server password")
    parser.add_argument(
        "--base_url", default="http://192.168.4.22:3030", help="Game server base URL"
    )
    parser.add_argument(
        "--opp_k", type=int, default=3,
        help="How many nearest opponents to model in minimax (3 = all in a "
             "4-snake match). Their bodies are always avoided regardless; this "
             "controls how many opponents' future moves we branch on.",
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
    # Cap HTTP at well under SEARCH_BUDGET so a stalled GET can't, by itself,
    # blow the half-tick cycle budget -- we abandon it and retry next tick.
    api = SnakeFieldAPI(
        args.base_url, args.team_name, args.game_name, args.password, timeout=0.3
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
