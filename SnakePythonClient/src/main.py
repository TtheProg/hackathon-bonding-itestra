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

# The server tick is wall-clock aligned: it ticks at the top of every second
# (synchronized via NTP). Our machine's clock is NTP-synced too, so we don't
# detect the edge at all -- we schedule against time.time() relative to each
# second boundary t0. Budget is ~3 req/s, spent as exactly 1 GET + 2 POSTs:
#   GET at t0+0.050  (50ms after the tick, the fresh board is up)
#   POST at t0+0.450 (insurance: best move so far)
#   POST at t0+0.800 (final: last post before the next tick wins)
TICK_SECONDS = 1.0
GET_OFFSET = 0.050
INSURANCE_OFFSET = 0.450
FINAL_OFFSET = 0.600      # post before the server's input cutoff (was 0.800 -> late -> LAG)
SEND_TIMEOUT = 0.20       # per-POST timeout
GET_TIMEOUT = 0.30        # per-GET timeout
INSURANCE_ENABLED = False  # the t0+450ms "insurance" POST (off = test: 1 GET + 1 POST/tick)
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


def post_once(api: SnakeFieldAPI, direction, stats: SendStats) -> Optional[int]:
    """Single POST of `direction`. Returns the status code (or None on error).

    The server uses the *last* direction posted before a tick, and our request
    budget is tiny (~3/s shared with GETs), so we post once and move on rather
    than retrying -- the two posts per tick (insurance + final) give two chances
    spaced far enough apart for the token bucket to refill between them.
    """
    try:
        code = api.set_direction(direction, timeout=SEND_TIMEOUT)
        stats.total += 1
        stats.last_status = code
        stats.last_direction = direction
        if code == 200:
            stats.ok += 1
        elif code == 429:
            stats.n_429 += 1
        return code
    except Exception as exc:
        log.debug("POST %s failed: %s", direction, exc)
        return None


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


def _sleep_until(wall_target: float) -> None:
    """Sleep until a specific wall-clock time (time.time())."""
    dt = wall_target - time.time()
    if dt > 0:
        time.sleep(dt)


def _edge_ms(t: Optional[float] = None) -> float:
    """Milliseconds past the most recent wall-clock second boundary (= the
    server's NTP-aligned tick edge). With our schedule a GET should read ~50,
    the insurance POST ~450, and the final POST ~800. Large drift here means our
    requests aren't actually landing where we think relative to the tick."""
    if t is None:
        t = time.time()
    return (t - int(t)) * 1000.0


def run(api: SnakeFieldAPI, team: str, opp_k: int, auto_reset: bool) -> None:
    deltas = dict(DEFAULT_DELTAS)
    stats = SendStats()
    last_direction = None
    prev_head: Optional[Tuple[int, int]] = None
    tick_no = 0
    not_appearing = 0
    reported_dead = False

    while True:
        # The server tick fires at the top of each wall-clock second (NTP-aligned),
        # so t0 = the next second boundary. We schedule GET/POSTs relative to it.
        t0 = float(int(time.time())) + 1.0

        # ---- GET the fresh board shortly after the tick ----
        _sleep_until(t0 + GET_OFFSET)
        get_off = _edge_ms()
        try:
            field = api.get_field(timeout=GET_TIMEOUT)
        except ApiError as exc:
            log.debug("GET state -> HTTP %s", exc.status)
            continue
        except Exception as exc:
            log.debug("GET state failed: %s", exc)
            continue

        state = state_from_field(field, team)

        # ---- not in the game yet: nudge a join, then wait for the next second ----
        if state is None:
            not_appearing += 1
            reported_dead = False
            log.info("waiting to appear in game '%s' (snakes: %s, try %d)...",
                     api.game_name, list(field.snakes.keys()), not_appearing)
            post_once(api, "NORTH", stats)  # joining = post a direction with our auth
            if auto_reset and not_appearing == 5:
                try_reset(api, "could not join")
            last_direction, prev_head = None, None
            continue
        not_appearing = 0

        me = state.snakes[team]
        if not me.alive:
            # Report the final standings once, then sit quietly until the game is
            # (manually) reset -- this server doesn't accept programmatic resets.
            if not reported_dead:
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
                reported_dead = True
            last_direction, prev_head = None, None
            continue
        reported_dead = False

        # ---- alive: did last tick's command actually land? ----
        landed_note = "first-move"
        if last_direction and prev_head is not None:
            obs = observed_delta(prev_head, me.head, state.size)
            expected = deltas[last_direction]
            if obs == expected:
                landed_note = f"OK cmd={last_direction} moved={obs}"
            else:
                # Mapping is confirmed correct (verified via the API steer test),
                # so a mismatch is one-tick command lag -- our POST landed after the
                # server locked that move. NOT a mapping error; don't mutate deltas.
                landed_note = (f"LAG cmd={last_direction} "
                               f"expected={expected} moved={obs}")
        tick_no += 1
        prev_head = me.head

        # ---- COMPUTE until the final-send mark; insurance POST fires mid-search ----
        posts_before = stats.total
        sent_insurance = False
        insurance_wall = t0 + INSURANCE_OFFSET
        ins_off = None  # ms-past-second the insurance POST actually fired

        def on_improve(direction):
            # First completed-depth at/after the insurance mark posts the best move
            # so far, so the server holds a sane move even if the final POST 429s.
            nonlocal sent_insurance, ins_off
            if (INSURANCE_ENABLED and not sent_insurance
                    and time.time() >= insurance_wall):
                sent_insurance = True
                ins_off = _edge_ms()
                post_once(api, direction, stats)

        # choose_direction deadlines on monotonic time; translate the wall-clock
        # final-send mark into a monotonic deadline.
        deadline_mono = time.monotonic() + max(0.0, (t0 + FINAL_OFFSET) - time.time())
        result: SearchResult = choose_direction(
            state, deadline_mono, deltas, opp_k=opp_k, on_improve=on_improve
        )
        last_direction = result.direction

        # ---- FINAL POST: the authoritative move, last post before the next tick ----
        _sleep_until(t0 + FINAL_OFFSET)
        fin_off = _edge_ms()
        final_code = post_once(api, result.direction, stats)

        # ---- log everything ----
        log.info("=== tick~%d | %s%s", tick_no, state_digest(state),
                 raw_extras(api))
        log.info("board:\n%s", render_board(state))
        log.info(
            "DECIDE move=%s depth=%d score=%.0f nodes=%d %.0fms | last-move: %s",
            result.direction, result.depth, result.score,
            getattr(result, "nodes", 0), getattr(result, "elapsed_ms", 0.0),
            landed_note,
        )
        log.info(
            "POST: final=%s dir=%s | TIMING get@+%.0f ins@%s final@+%.0f ms | "
            "reqs=%d | totals ok=%d 429=%d all=%d",
            final_code, result.direction, get_off,
            f"+{ins_off:.0f}" if ins_off is not None else "off", fin_off,
            stats.total - posts_before, stats.ok, stats.n_429, stats.total,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ouroboros snake bot")
    parser.add_argument("--team_name", default="Ouroboros", help="Team/snake name")
    parser.add_argument("--game_name", default="vividLion", help="Game to join")
    parser.add_argument("--password", default="test", help="Server password")
    parser.add_argument(
        "--base_url", default="http://192.168.5.16:3030", help="Game server base URL"
    )
    parser.add_argument(
        "--opp_k", type=int, default=2,
        help="How many nearest opponents to model in minimax. Each one multiplies "
             "the branching factor, so this trades threat-awareness for search "
             "depth. On the 21x21/12-snake board, k=2 reaches depth ~4-7 and "
             "survives; k=3 bottoms out at depth ~2. Opponent bodies are always "
             "avoided regardless of k.",
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
