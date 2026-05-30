"""Quick offline sanity checks for the engine. Run: python _sanity.py"""
from engine import (
    DEFAULT_DELTAS, SimSnake, SimState, choose_direction, simulate,
    state_from_field,
)
import time

D = DEFAULT_DELTAS


def st(snakes, apples=(), bad_apples=(), size=(10, 10), me="me"):
    return SimState(size=size, snakes={n: SimSnake(list(b)) for n, b in snakes.items()},
                    apples=set(apples), bad_apples=set(bad_apples), me=me)


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


def test_never_reverses():
    # heading EAST (head (5,5), neck (4,5)); apple due WEST (behind us).
    # Reversing to WEST = instant death, so it must NOT choose WEST even though
    # the apple is that way.
    s = st({"me": [(5, 5), (4, 5), (3, 5)]}, apples=[(2, 5)])
    assert "WEST" not in [d for d in __import__("engine").legal_moves(
        s.snakes["me"], D, s.size)], "WEST (reverse) must be illegal"
    res = choose_direction(s, time.monotonic() + 0.5, D, opp_k=0)
    assert res.direction != "WEST", f"reversed into neck! chose {res.direction}"
    print(f"ok: never reverses -> chose {res.direction}")


def test_avoids_opponent_body():
    # We head EAST straight into an opponent's body wall; must turn away.
    s = st({
        "me": [(4, 5), (3, 5)],
        "opp": [(5, 3), (5, 4), (5, 5), (5, 6), (5, 7)],  # vertical wall at x=5
    })
    res = choose_direction(s, time.monotonic() + 0.5, D, opp_k=1)
    assert res.direction != "EAST", f"ran into opponent wall! chose {res.direction}"
    print(f"ok: avoids opponent body -> chose {res.direction}")


def test_seeks_apple():
    s = st({"me": [(5, 5), (5, 6)]}, apples=[(2, 5)])  # apple to the west
    t0 = time.monotonic()
    res = choose_direction(s, deadline=t0 + 0.5, deltas=D, opp_k=0)
    print(f"ok: apple-seek -> chose {res.direction} (apple west, expect WEST-ish)")


def test_safe_filter_survives_bad_calibration():
    """The hard safety gate must hold even if the compass map is WRONG.

    Reproduces the real-game failure: a momentarily mis-calibrated `deltas` makes
    `legal_moves`' reversal check mislabel directions, so the reversal-into-neck
    move slips through. `safe_moves` judges the destination cell instead, so we
    must still refuse to step onto our own neck regardless of the bad map.
    """
    from engine import safe_moves
    # Head (5,5), neck (4,5): the snake came from the WEST, so stepping WEST is
    # reversal into the neck. Feed a SCRAMBLED delta map (compass labels rotated)
    # to prove safety doesn't depend on it being correct.
    s = st({"me": [(5, 5), (4, 5), (3, 5), (2, 5)]})
    scrambled = {"NORTH": (-1, 0), "SOUTH": (1, 0), "EAST": (0, 1), "WEST": (0, -1)}
    safe = safe_moves(s, scrambled)
    # Whatever label maps to (-1,0) (a step onto the neck at (4,5)) must be gone.
    for d in safe:
        nh = ((5 + scrambled[d][0]) % 10, (5 + scrambled[d][1]) % 10)
        assert nh not in {(5, 5), (4, 5), (3, 5), (2, 5)}, f"{d} steps onto body {nh}"
    res = choose_direction(s, time.monotonic() + 0.3, scrambled, opp_k=0)
    rh = ((5 + scrambled[res.direction][0]) % 10, (5 + scrambled[res.direction][1]) % 10)
    assert rh not in {(4, 5), (3, 5), (2, 5)}, f"chose {res.direction} into body {rh}"
    print(f"ok: safe filter survives bad calibration -> chose {res.direction}")


if __name__ == "__main__":
    test_move_and_wrap()
    test_growth()
    test_self_collision()
    test_head_to_head()
    test_avoids_death()
    test_never_reverses()
    test_avoids_opponent_body()
    test_seeks_apple()
    test_safe_filter_survives_bad_calibration()
    print("ALL SANITY CHECKS PASSED")
