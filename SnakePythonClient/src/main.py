"""Ouroboros snake bot.

The loop is **phase-locked to the server's tick** instead of free-running at a
fixed cadence. A free-running ~1 Hz loop drifts against the server's tick edges
(the server ticks at ~1/sec, only approximately), and that drift is what made our
POSTs land *after* the tick locked (the old "LAG" failure). Each tick we instead:

  1. DETECT the tick edge by polling GET /state every 50 ms until the board
     changes (`tick_token`), then anchor `t0 = now`.
  2. CALIBRATE: verify last tick's command actually moved our head as expected.
  3. COMPUTE: one anytime iterative-deepening paranoid minimax to `t0 + 0.75 s`.
     An insurance POST of the best-so-far fires from inside the search once the
     clock passes `t0 + 0.40 s` (so the server always holds a sane move).
  4. SEND: POST the final chosen move in the `[t0+0.75, t0+0.85]` window,
     retrying until HTTP 200. Then resume polling for the next edge.

The server token-buckets requests (HTTP 429 on bursts), but phase-locking keeps
us gentle: ~2-3 detection GETs clustered at the edge plus 1-2 POSTs, with a quiet
~750 ms compute gap in between. Only the last POST before a tick counts.

Logging: every tick logs the board, all snakes, the decision (move/depth/score),
the POST status, and whether last tick's command actually moved our head. Logs
go to the console and to ../logs/ouroboros-<timestamp>.log.
"""
import argparse
import logging
import os
import time
from datetime import datetime
from typing import Optional, Tuple

from api import ApiError, SnakeFieldAPI
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
POLL_INTERVAL = 0.05      # detection GET cadence while hunting the tick edge
EARLY_SEND_MARK = 0.40    # fire the insurance post at t0 + this (from on_improve)
COMPUTE_BUDGET = 0.75     # total minimax deadline measured from the tick edge t0
FINAL_SEND_BUDGET = 0.10  # window [t0+0.75, t0+0.85] to land a 200
SEND_TIMEOUT = 0.08       # per-POST timeout in both send phases
DETECT_TIMEOUT = 0.08     # per-GET timeout while polling for the edge
# Keys the server might use for a turn counter; preferred over the head-hash
# edge signal if one actually shows up in the raw /state payload.
_TICK_KEYS = ("tick", "turn", "round", "step", "frame")


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


def tick_token(field, api: SnakeFieldAPI):
    """A value that changes exactly once per server tick, used to detect the
    tick edge while polling.

    Prefers an explicit turn counter if the raw /state payload exposes one (we
    can't confirm the server does without the live endpoint, so this is just
    opportunistic). Otherwise falls back to a hash of every snake's head
    position + alive flag: every alive snake's head moves each tick and the
    server updates the board atomically, so the value is stable between ticks
    and changes at the edge.
    """
    raw = getattr(api, "last_raw", None)
    if isinstance(raw, dict):
        for k in _TICK_KEYS:
            v = raw.get(k)
            if isinstance(v, int):
                return ("counter", k, v)
    heads = tuple(sorted(
        (name, info.head, info.alive) for name, info in field.snakes.items()
    ))
    return ("heads", heads)


class SendStats:
    """Running POST tallies, just for the per-tick log line."""

    def __init__(self) -> None:
        self.last_status: Optional[int] = None
        self.last_direction = None
        self.total = 0
        self.ok = 0
        self.n_429 = 0


def send_until_ok(api: SnakeFieldAPI, direction, deadline: float,
                  stats: SendStats) -> bool:
    """POST `direction` (short timeout) until HTTP 200 or `deadline` passes.

    One 200 registers the move for the next tick, so we stop on the first. A
    small gap between attempts keeps a fast-429ing server from being hammered.
    Returns True if a 200 landed.
    """
    while True:
        try:
            code = api.set_direction(direction, timeout=SEND_TIMEOUT)
            stats.total += 1
            stats.last_status = code
            stats.last_direction = direction
            if code == 200:
                stats.ok += 1
                return True
            if code == 429:
                stats.n_429 += 1
        except Exception as exc:
            log.debug("POST %s failed: %s", direction, exc)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.04)


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
    deltas = dict(DEFAULT_DELTAS)
    stats = SendStats()
    last_direction = None
    prev_head: Optional[Tuple[int, int]] = None
    prev_token = None
    tick_no = 0
    not_appearing = 0

    while True:
        # ---- DETECT: poll until the board advances to a new tick edge ----
        # While alive and joined, GET fast (every POLL_INTERVAL) until tick_token
        # changes, then anchor t0 = now. A None state (not joined) or a dead
        # snake has no edge to lock to, so we break out and handle it below.
        t0 = None
        field = state = None
        while True:
            try:
                field = api.get_field(timeout=DETECT_TIMEOUT)
            except ApiError as exc:
                # A 429 here must NOT blind us with a long backoff -- retry fast.
                time.sleep(0.03 if exc.status == 429 else 0.1)
                if exc.status != 429:
                    log.debug("GET state -> %s", exc)
                continue
            except Exception as exc:
                log.debug("GET state failed: %s", exc)
                time.sleep(0.1)
                continue

            state = state_from_field(field, team)
            if state is None or not state.snakes[team].alive:
                break  # handled outside the detect loop

            token = tick_token(field, api)
            if prev_token is None or token != prev_token:
                prev_token = token
                t0 = time.monotonic()
                break
            time.sleep(POLL_INTERVAL)  # same tick: wait for the board to advance

        # ---- not in the game yet ----
        if state is None:
            not_appearing += 1
            log.info("waiting to appear in game '%s' (snakes: %s, try %d)...",
                     api.game_name, list(field.snakes.keys()), not_appearing)
            try:
                api.set_direction("NORTH", timeout=SEND_TIMEOUT)  # nudge join
            except Exception:
                pass
            if auto_reset:
                if not_appearing == 5:
                    try_reset(api, "could not join")
                elif not_appearing >= 10:
                    try_recreate(api, "join still failing after reset")
                    not_appearing = 0
            prev_token, last_direction, prev_head = None, None, None
            continue

        me = state.snakes[team]
        if not me.alive:
            # Log final standings ("game end and state"); auto-reset restarts now
            # so we don't sit in a game that may otherwise run forever.
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
            prev_token, last_direction, prev_head = None, None, None
            if auto_reset:
                time.sleep(1.0)
                try_reset(api, "our snake died")
                try:
                    api.set_direction("NORTH", timeout=SEND_TIMEOUT)  # auto-start
                except Exception:
                    pass
            else:
                time.sleep(1.0)
            continue

        # ---- tick state, anchored at t0, and we are alive ----
        # --- did last tick's command actually land? ---
        landed_note = "first-move"
        if last_direction and prev_head is not None:
            obs = observed_delta(prev_head, me.head, state.size)
            expected = deltas[last_direction]
            if obs == expected:
                landed_note = f"OK cmd={last_direction} moved={obs}"
            else:
                # Mapping is confirmed correct (verified via the API steer test),
                # so a mismatch is the one-tick command lag -- our POST landed
                # after the server locked that tick's move. NOT a mapping error;
                # do not mutate the deltas (that caused calibration churn).
                landed_note = (f"LAG cmd={last_direction} "
                               f"expected={expected} moved={obs}")
        tick_no += 1
        prev_head = me.head

        # ---- COMPUTE: one full-depth search; insurance post fires from inside ----
        posts_before = stats.total
        sent_insurance = False

        def on_improve(direction):
            # First time the search has a best move at/after the 400 ms mark,
            # fire a single quick insurance POST so the server holds a sane move
            # even if the final send later 429s. One attempt only -- this blocks
            # the search for the POST, so we don't retry here.
            nonlocal sent_insurance
            if not sent_insurance and time.monotonic() >= t0 + EARLY_SEND_MARK:
                sent_insurance = True
                try:
                    code = api.set_direction(direction, timeout=SEND_TIMEOUT)
                    stats.total += 1
                    stats.last_status = code
                    stats.last_direction = direction
                    if code == 200:
                        stats.ok += 1
                    elif code == 429:
                        stats.n_429 += 1
                except Exception as exc:
                    log.debug("insurance POST failed: %s", exc)

        result: SearchResult = choose_direction(
            state, t0 + COMPUTE_BUDGET, deltas, opp_k=opp_k, on_improve=on_improve
        )
        last_direction = result.direction

        # ---- FINAL SEND: land the authoritative move before the next tick ----
        landed = send_until_ok(
            api, result.direction, t0 + COMPUTE_BUDGET + FINAL_SEND_BUDGET, stats
        )

        # ---- log everything ----
        log.info("=== tick~%d | %s%s", tick_no, state_digest(state),
                 raw_extras(api))
        log.info("board:\n%s", render_board(state))
        log.info(
            "DECIDE move=%s depth=%d score=%.0f | last-move: %s",
            result.direction, result.depth, result.score, landed_note,
        )
        log.info(
            "POST: final=%s last=%s dir=%s | insurance=%s | totals ok=%d 429=%d "
            "all=%d (+%d this tick)",
            "200" if landed else "MISS", stats.last_status, stats.last_direction,
            "yes" if sent_insurance else "no", stats.ok, stats.n_429, stats.total,
            stats.total - posts_before,
        )
        # No trailing sleep: the detect poll above naturally waits for the next
        # tick edge, re-anchoring t0 every tick.


def main() -> None:
    parser = argparse.ArgumentParser(description="Ouroboros snake bot")
    parser.add_argument("--team_name", default="Ouroboros", help="Team/snake name")
    parser.add_argument("--game_name", default="BracketA", help="Game to join")
    parser.add_argument("--password", default="hermeticism", help="Server password")
    parser.add_argument(
        "--base_url", default="http://192.168.3.13:3030", help="Game server base URL"
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
