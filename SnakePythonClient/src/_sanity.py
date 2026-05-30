"""Quick offline sanity checks for the engine. Run: python _sanity.py"""
from engine import (
    DEFAULT_DELTAS, SimSnake, SimState, choose_direction, simulate,
    state_from_field,
)
import time

D = DEFAULT_DELTAS


def st(snakes, apples=(), size=(10, 10), me="me"):
    return SimState(size=size, snakes={n: SimSnake(list(b)) for n, b in snakes.items()},
                    apples=set(apples), me=me)


def test_move_and_wrap():
    s = st({"me": [(5, 5), (5, 6)]})  # heading north
    nx = simulate(s, {"me": "NORTH"}, D)
    assert nx.snakes["me"].head == (5, 4), nx.snakes["me"].head
    # wrap across top edge
    s = st({"me": [(5, 0), (5, 1)]})
    nx = simulate(s, {"me": "NORTH"}, D)
    assert nx.snakes["me"].head == (5, 9), nx.snakes["me"].head
    print("ok: movement + wrap")


def test_growth():
    s = st({"me": [(5, 5), (5, 6)]}, apples=[(5, 4)])
    nx = simulate(s, {"me": "NORTH"}, D)
    assert nx.snakes["me"].length == 3, nx.snakes["me"].length
    assert (5, 4) not in nx.apples
    print("ok: apple growth")


def test_self_collision():
    # square loop; moving into own body kills
    body = [(5, 5), (4, 5), (4, 6), (5, 6)]  # head (5,5)
    s = st({"me": body})
    nx = simulate(s, {"me": "SOUTH"}, D)  # into (5,6) which is body tail-ish
    # (5,6) is current tail; tail moves away so it's actually free -> survives
    assert nx.snakes["me"].alive, "tail vacates -> should survive"
    print("ok: tail-chase survives")


def test_head_to_head():
    s = st({"me": [(4, 5), (3, 5)], "opp": [(6, 5), (7, 5)]})
    nx = simulate(s, {"me": "EAST", "opp": "WEST"}, D)  # both -> (5,5)
    assert not nx.snakes["me"].alive and not nx.snakes["opp"].alive
    print("ok: head-to-head double death")


def test_avoids_death():
    # me boxed except one safe exit; ensure we don't pick the suicidal wall of opp
    s = st({
        "me": [(5, 5), (5, 6)],
        "opp": [(5, 4), (5, 3), (5, 2)],  # body straight ahead (north)
    })
    t0 = time.monotonic()
    res = choose_direction(s, deadline=t0 + 0.5, deltas=D, opp_k=1)
    # NORTH walks into opp body at (5,4) -> should be avoided
    assert res.direction != "NORTH", res.direction
    print(f"ok: avoids death -> chose {res.direction} depth={res.depth}")


def test_seeks_apple():
    s = st({"me": [(5, 5), (5, 6)]}, apples=[(2, 5)])  # apple to the west
    t0 = time.monotonic()
    res = choose_direction(s, deadline=t0 + 0.5, deltas=D, opp_k=0)
    print(f"ok: apple-seek -> chose {res.direction} (apple west, expect WEST-ish)")


if __name__ == "__main__":
    test_move_and_wrap()
    test_growth()
    test_self_collision()
    test_head_to_head()
    test_avoids_death()
    test_seeks_apple()
    print("ALL SANITY CHECKS PASSED")
