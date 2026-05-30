"""Game engine: torus simulation + flood-fill heuristic + anytime minimax.

The bot is deliberately decoupled from the wire format (see Field.py). All the
search operates on plain `SimState` objects so the strategy keeps working even
if the server's JSON shape changes.

Direction -> (dx, dy) deltas are NOT hard-coded as ground truth: `DEFAULT_DELTAS`
is only a starting guess. main.py calibrates the real mapping at runtime by
observing how our own head actually moves, and passes the corrected mapping in.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from data_structures import Coord, Direction, get_directions_as_list

# Screen/canvas convention from the arena GUI: x grows east, y grows south.
# NORTH = up = y-1. This is only the default; it gets calibrated live.
DEFAULT_DELTAS: Dict[Direction, Tuple[int, int]] = {
    "NORTH": (0, -1),
    "SOUTH": (0, 1),
    "EAST": (1, 0),
    "WEST": (-1, 0),
}

DIRECTIONS: List[Direction] = get_directions_as_list()

INF = float("inf")


# --------------------------------------------------------------------------- #
# Simulation state
# --------------------------------------------------------------------------- #
@dataclass
class SimSnake:
    body: List[Coord]
    alive: bool = True

    @property
    def head(self) -> Coord:
        return self.body[0]

    @property
    def length(self) -> int:
        return len(self.body)

    def heading(self, size: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        """Unit (dx, dy) the head moved last tick, or None if unknown."""
        if len(self.body) < 2 or self.body[0] == self.body[1]:
            return None
        w, h = size
        dx = _wrap_delta(self.body[0][0] - self.body[1][0], w)
        dy = _wrap_delta(self.body[0][1] - self.body[1][1], h)
        return (dx, dy)

    def clone(self) -> "SimSnake":
        return SimSnake(body=list(self.body), alive=self.alive)


@dataclass
class SimState:
    size: Tuple[int, int]
    snakes: Dict[str, SimSnake]
    apples: set
    me: str

    def clone(self) -> "SimState":
        return SimState(
            size=self.size,
            snakes={n: s.clone() for n, s in self.snakes.items()},
            apples=set(self.apples),
            me=self.me,
        )

    def alive_names(self) -> List[str]:
        return [n for n, s in self.snakes.items() if s.alive]


def _wrap_delta(d: int, span: int) -> int:
    """Map a raw coordinate difference to -1/0/+1 across a torus edge."""
    if d > span // 2:
        d -= span
    elif d < -(span // 2):
        d += span
    if d > 0:
        return 1
    if d < 0:
        return -1
    return 0


def wrap(pos: Coord, size: Tuple[int, int]) -> Coord:
    w, h = size
    return (pos[0] % w, pos[1] % h)


def step(head: Coord, direction: Direction, deltas, size) -> Coord:
    dx, dy = deltas[direction]
    return wrap((head[0] + dx, head[1] + dy), size)


def legal_moves(snake: SimSnake, deltas, size) -> List[Direction]:
    """All directions except an immediate reversal into our own neck."""
    heading = snake.heading(size)
    moves = []
    for d in DIRECTIONS:
        dx, dy = deltas[d]
        if heading is not None and (dx, dy) == (-heading[0], -heading[1]):
            continue  # reversing is instant suicide
        moves.append(d)
    return moves or list(DIRECTIONS)


# --------------------------------------------------------------------------- #
# Tick simulation (simultaneous movement + collisions)
# --------------------------------------------------------------------------- #
def simulate(state: SimState, moves: Dict[str, Direction], deltas) -> SimState:
    """Advance one tick. `moves` maps snake name -> direction for alive snakes.

    Order per the spec: move all heads simultaneously, grow on apples (tail kept
    when an apple is eaten, else dropped), then resolve every head/body
    collision simultaneously. New item spawns are not modelled (positions are
    random and unknown to us).
    """
    nxt = state.clone()
    size = state.size

    new_heads: Dict[str, Coord] = {}
    for name, snake in nxt.snakes.items():
        if not snake.alive:
            continue
        d = moves.get(name)
        if d is None:
            # Keep going straight if we have no instruction for this snake.
            heading = snake.heading(size)
            new_heads[name] = (
                wrap((snake.head[0] + heading[0], snake.head[1] + heading[1]), size)
                if heading
                else snake.head
            )
        else:
            new_heads[name] = step(snake.head, d, deltas, size)

    # Apply movement + growth.
    for name, snake in nxt.snakes.items():
        if not snake.alive:
            continue
        head = new_heads[name]
        grew = head in nxt.apples
        snake.body = [head] + snake.body if grew else [head] + snake.body[:-1]
        if grew:
            nxt.apples.discard(head)

    # Resolve collisions against post-move bodies, all at once.
    occupied: Dict[Coord, int] = {}
    bodies: Dict[str, List[Coord]] = {}
    for name, snake in nxt.snakes.items():
        if not snake.alive:
            continue
        bodies[name] = snake.body
        for cell in snake.body:
            occupied[cell] = occupied.get(cell, 0) + 1

    dead = []
    for name, snake in nxt.snakes.items():
        if not snake.alive:
            continue
        head = snake.head
        # Head shares a cell with some body segment (ours beyond the head, or
        # any other snake's segment, which also covers head-to-head).
        own = bodies[name].count(head)
        if occupied[head] - own > 0 or own > 1:
            dead.append(name)
    for name in dead:
        nxt.snakes[name].alive = False

    return nxt


# --------------------------------------------------------------------------- #
# Heuristics
# --------------------------------------------------------------------------- #
def torus_dist(a: Coord, b: Coord, size: Tuple[int, int]) -> int:
    w, h = size
    dx = abs(a[0] - b[0])
    dy = abs(a[1] - b[1])
    return min(dx, w - dx) + min(dy, h - dy)


def flood_fill(start: Coord, blocked: set, size: Tuple[int, int], limit: int) -> int:
    """Count free cells reachable from `start` (the cell the head moves into)."""
    count, _, _ = bfs_field(start, blocked, size, frozenset(), limit)
    return count


def bfs_field(start: Coord, blocked: set, size: Tuple[int, int],
              targets, limit: int):
    """Single breadth-first sweep from `start` over free cells (torus).

    Returns (reachable_count, nearest_target, nearest_dist). Because every step
    costs 1, BFS *is* the optimal-path distance (A* with a zero/admissible
    heuristic collapses to this), and one sweep from the head yields the true
    obstacle-avoiding distance to the closest reachable target -- cheaper and
    more complete than running A* per target. Targets (apples) are passable, so
    we still count them as reachable space.
    """
    w, h = size
    seen = {start}
    q = deque([(start, 0)])
    count = 0
    nearest = None
    nearest_dist = None
    while q and count < limit:
        (x, y), dist = q.popleft()
        count += 1
        if nearest is None and dist > 0 and (x, y) in targets:
            nearest, nearest_dist = (x, y), dist
        for dx, dy in ((0, -1), (0, 1), (1, 0), (-1, 0)):
            nb = ((x + dx) % w, (y + dy) % h)
            if nb not in seen and nb not in blocked:
                seen.add(nb)
                q.append((nb, dist + 1))
    return count, nearest, nearest_dist


def evaluate(state: SimState, depth_left: int) -> float:
    """Score the position from our point of view. Higher is better."""
    me = state.snakes[state.me]
    size = state.size
    cells = size[0] * size[1]

    if not me.alive:
        # Dying is terrible; dying sooner is worse than dying later, so reward
        # the extra plies survived (depth_left is high near the root).
        return -1e9 - depth_left * 1e6

    # Free space reachable from our head = anti-trap signal. Block every snake
    # body, but NOT our own head cell (that's where we measure *from* -- leaving
    # it in `blocked` made flood_fill start on a blocked cell and always return
    # 0, silently disabling this whole term).
    blocked = set()
    for s in state.snakes.values():
        if s.alive:
            blocked.update(s.body)
    blocked.discard(me.head)

    # One BFS gives both the reachable space (anti-trap) and the true
    # obstacle-avoiding path distance to the nearest reachable apple.
    space, _, apple_dist = bfs_field(me.head, blocked, size, state.apples, cells)
    score = 0.0
    score += me.length * 1000.0          # length is the literal scoreboard
    score += space * 10.0                # don't get boxed in
    # If we can't even reach as many cells as our own length, we're trapped.
    if space < me.length:
        score -= (me.length - space) * 200.0

    # Seek apples by *path* distance. Weighted strongly enough that closing the
    # distance beats coasting straight, but below the +1000 of eating (via the
    # length term once the head reaches the apple in a child state).
    if state.apples:
        if apple_dist is not None:
            score -= apple_dist * 30.0
        else:
            # No free path to any apple right now (bodies in the way): keep a
            # weaker straight-line pull plus a penalty for being walled off.
            nearest = min(torus_dist(me.head, a, size) for a in state.apples)
            score -= nearest * 30.0 + 80.0

    # Mild bonus for outliving opponents.
    opponents_alive = sum(
        1 for n, s in state.snakes.items() if n != state.me and s.alive
    )
    score -= opponents_alive * 50.0

    # Avoid sitting adjacent to a longer/equal enemy head (head-to-head risk).
    for n, s in state.snakes.items():
        if n == state.me or not s.alive:
            continue
        if torus_dist(me.head, s.head, size) == 1 and s.length >= me.length:
            score -= 120.0

    return score


# --------------------------------------------------------------------------- #
# Anytime paranoid minimax with iterative deepening
# --------------------------------------------------------------------------- #
class TimeUp(Exception):
    pass


@dataclass
class SearchResult:
    direction: Direction
    score: float
    depth: int
    completed: bool


# Only branch on opponents whose head is within this many cells of ours: a
# snake further away cannot collide with us inside the search horizon, so
# enumerating its moves only burns time (its body is still always an obstacle).
# This lets the search go deep when enemies are far and stay careful when close.
THREAT_RADIUS = 6


def _nearby_opponents(state: SimState, k: int,
                      max_dist: int = THREAT_RADIUS) -> List[str]:
    me = state.snakes[state.me]
    others = [
        (d, n)
        for n, s in state.snakes.items()
        if n != state.me and s.alive
        and (d := torus_dist(me.head, s.head, state.size)) <= max_dist
    ]
    others.sort()
    return [n for _, n in others[:k]]


def _opponent_joint_moves(state: SimState, opponents: List[str], deltas):
    """Cartesian product of each modelled opponent's non-suicidal moves."""
    per_snake = []
    for n in opponents:
        per_snake.append((n, legal_moves(state.snakes[n], deltas, state.size)))

    combos = [{}]
    for name, moves in per_snake:
        combos = [dict(c, **{name: m}) for c in combos for m in moves]
    return combos or [{}]


def _search(state: SimState, depth: int, alpha: float, beta: float,
            deadline: float, deltas, opp_k: int) -> float:
    if time.monotonic() > deadline:
        raise TimeUp
    me = state.snakes[state.me]
    if not me.alive or len(state.alive_names()) <= 1 or depth == 0:
        return evaluate(state, depth)

    opponents = _nearby_opponents(state, opp_k)
    combos = _opponent_joint_moves(state, opponents, deltas)

    best = -INF
    for my_move in legal_moves(me, deltas, state.size):
        # Paranoid: opponents jointly pick the response worst for us.
        worst = INF
        for opp_moves in combos:
            moves = dict(opp_moves)
            moves[state.me] = my_move
            child = simulate(state, moves, deltas)
            val = _search(child, depth - 1, alpha, beta, deadline, deltas, opp_k)
            if val < worst:
                worst = val
            if worst <= alpha:
                break  # this move already refuted; prune
        if worst > best:
            best = worst
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def choose_direction(state: SimState, deadline: float, deltas,
                     max_depth: int = 12, opp_k: int = 2,
                     on_improve=None) -> SearchResult:
    """Iterative-deepening anytime search.

    Returns the best move found before `deadline`. `on_improve(direction)` is
    called every time a deeper, completed search yields a (possibly new) best
    move, so the caller can post it immediately.
    """
    me = state.snakes[state.me]
    moves = legal_moves(me, deltas, state.size)

    # Move ordering = the tie-break (the search keeps the first move on equal
    # scores). Commit to ONE target apple -- the deterministically-nearest one --
    # and try the move that gets closest to it first. Without this, apples
    # flanking the head leave every move equally "good", so the snake defers the
    # turn every tick and never actually eats. Fall back to going straight.
    heading = me.heading(state.size)
    target = None
    if state.apples:
        # Prefer the nearest apple reachable by an actual path (BFS around
        # bodies); fall back to straight-line nearest if all are walled off.
        blocked = set()
        for s in state.snakes.values():
            if s.alive:
                blocked.update(s.body)
        blocked.discard(me.head)
        _, target, _ = bfs_field(me.head, blocked, state.size, state.apples,
                                 state.size[0] * state.size[1])
        if target is None:
            target = min(state.apples,
                         key=lambda a: (torus_dist(me.head, a, state.size),
                                        a[0], a[1]))

    def order_key(d: Direction):
        nh = step(me.head, d, deltas, state.size)
        to_target = torus_dist(nh, target, state.size) if target else 0
        straight_pref = 0 if (heading and deltas[d] == heading) else 1
        return (to_target, straight_pref)

    moves.sort(key=order_key)

    # Greedy fallback so we always have *some* answer instantly: the move that
    # maximises immediate free space (and nudges toward apples).
    def shallow_key(d: Direction) -> float:
        child = simulate(state, {state.me: d}, deltas)
        return evaluate(child, 0)

    best_dir = max(moves, key=shallow_key)
    best_score = -INF
    best_depth = 0
    if on_improve:
        on_improve(best_dir)

    opponents_present = len(state.alive_names()) > 1
    for depth in range(1, max_depth + 1):
        try:
            depth_best_dir = best_dir
            depth_best_score = -INF
            alpha = -INF
            for my_move in moves:
                if opponents_present:
                    opps = _nearby_opponents(state, opp_k)
                    combos = _opponent_joint_moves(state, opps, deltas)
                    worst = INF
                    for opp_moves in combos:
                        mv = dict(opp_moves)
                        mv[state.me] = my_move
                        child = simulate(state, mv, deltas)
                        val = _search(child, depth - 1, alpha, INF,
                                      deadline, deltas, opp_k)
                        worst = min(worst, val)
                        if worst <= alpha:
                            break
                    val = worst
                else:
                    child = simulate(state, {state.me: my_move}, deltas)
                    val = _search(child, depth - 1, alpha, INF,
                                  deadline, deltas, opp_k)
                if val > depth_best_score:
                    depth_best_score = val
                    depth_best_dir = my_move
                    alpha = max(alpha, val)
        except TimeUp:
            break

        best_dir, best_score, best_depth = depth_best_dir, depth_best_score, depth
        if on_improve:
            on_improve(best_dir)
        # If our best line already means certain death, deeper search won't help.
        if best_score <= -1e8:
            break

    return SearchResult(best_dir, best_score, best_depth, completed=True)


# --------------------------------------------------------------------------- #
# Bridge from the wire Field to a SimState
# --------------------------------------------------------------------------- #
def observed_delta(prev_head: Coord, new_head: Coord, size: Tuple[int, int]) -> Tuple[int, int]:
    """The (dx, dy) the head actually moved between two ticks, wrapped to the
    nearest edge so torus wrap reads as -1/+1 rather than a big jump."""
    w, h = size
    dx = ((new_head[0] - prev_head[0] + w // 2) % w) - w // 2
    dy = ((new_head[1] - prev_head[1] + h // 2) % h) - h // 2
    return (dx, dy)


def render_board(state: SimState) -> str:
    """ASCII view of the field for logging. Our head '@', body 'o'; each
    opponent gets a letter (HEAD uppercase, body lowercase); apples '*'."""
    w, h = state.size
    grid = [["." for _ in range(w)] for _ in range(h)]
    for a in state.apples:
        grid[a[1] % h][a[0] % w] = "*"
    letter = ord("A")
    for name, s in state.snakes.items():
        if not s.alive or not s.body:
            continue
        if name == state.me:
            body_ch, head_ch = "o", "@"
        else:
            head_ch = chr(letter)
            body_ch = head_ch.lower()
            letter = letter + 1 if letter < ord("Z") else letter
        for seg in s.body[1:]:
            grid[seg[1] % h][seg[0] % w] = body_ch
        head = s.body[0]
        grid[head[1] % h][head[0] % w] = head_ch
    rows = ["".join(row) for row in grid]
    return "\n".join(rows)


def state_from_field(field_obj, me: str) -> Optional[SimState]:
    if me not in field_obj.snakes:
        return None
    snakes = {
        name: SimSnake(body=list(info.body), alive=info.alive)
        for name, info in field_obj.snakes.items()
    }
    return SimState(
        size=tuple(field_obj.size),
        snakes=snakes,
        apples=set(field_obj.apples()),
        me=me,
    )
