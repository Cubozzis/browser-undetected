#!/usr/bin/env python3
"""Offline checks for the human-input model. No browser, no dependencies.

    python3 tests/test_human.py

A FakePage records what would have been dispatched, so the real Human code
paths (including the typo/backspace logic) run without Chrome in the loop.
"""
from __future__ import annotations

import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "browser-undetected", "scripts"))

import human as H  # noqa: E402


# --------------------------------------------------------------------------
# a stand-in for a Playwright Page
# --------------------------------------------------------------------------

class FakeMouse:
    def __init__(self, log):
        self.log = log

    def move(self, x, y):
        self.log.append(("move", round(x, 3), round(y, 3)))

    def down(self, button="left"):
        self.log.append(("down", button))

    def up(self, button="left"):
        self.log.append(("up", button))

    def click(self, x, y, click_count=1):
        self.log.append(("click", click_count, round(x, 3), round(y, 3)))

    def wheel(self, dx, dy):
        self.log.append(("wheel", dy))


class FakeKeyboard:
    def __init__(self, log):
        self.log = log
        self._down = False

    def type(self, text):
        for ch in text:
            self.log.append(("type", ch))

    def press(self, key):
        self.log.append(("press", key))


class FakeElement:
    def __init__(self, box=(100, 100, 120, 40)):
        self._box = {"x": box[0], "y": box[1], "width": box[2], "height": box[3]}
        self._checked = False

    def bounding_box(self):
        return dict(self._box)

    def scroll_into_view_if_needed(self, **_):
        pass

    def is_checked(self):
        return self._checked


class FakePage:
    def __init__(self, els=1):
        self.log = []
        self.mouse = FakeMouse(self.log)
        self.keyboard = FakeKeyboard(self.log)
        self._els = [FakeElement() for _ in range(els)]

    def query_selector_all(self, sel):
        return list(self._els)


def fake_page(els=1):
    p = FakePage(els)
    return p, p.log


# --------------------------------------------------------------------------
# path()
# --------------------------------------------------------------------------

def test_path_terminates_on_target():
    rng = random.Random(7)
    for target in [(500, 400), (0, 0), (1000, 800), (-50, 200)]:
        pts = H.path(12, 34, target[0], target[1], rng)
        assert pts, "path returned no points"
        assert pts[-1] == (float(target[0]), float(target[1])), \
            f"path ended at {pts[-1]}, expected {target}"
        for x, y in pts:
            assert math.isfinite(x) and math.isfinite(y), "non-finite coordinate"


def test_path_short_distance_and_no_div_by_zero():
    rng = random.Random(1)
    for d in [(10, 10, 10, 10), (10, 10, 10.5, 10.5), (0, 0, 1, 1)]:
        pts = H.path(*d, rng=rng)
        assert len(pts) >= 1
        assert pts[-1] == (float(d[2]), float(d[3]))


def test_path_bows_off_the_straight_line():
    """A haptic path should not be a straight line; check at least one sample
    departs measurably from the chord."""
    rng = random.Random(3)
    pts = H.path(0, 0, 600, 0, rng)
    assert max(abs(y) for _, y in pts) > 1.0, "path was perfectly straight"


# --------------------------------------------------------------------------
# durations()
# --------------------------------------------------------------------------

def test_durations_sum_matches_total():
    rng = random.Random(11)
    for total, steps in [(500, 20), (120, 8), (1000, 40), (60, 3)]:
        ds = H.durations(total, steps, rng)
        assert len(ds) == steps
        assert all(d >= 0 for d in ds), "negative duration"
        # jitter is per-step, so the sum lands near the total, not exactly on it
        assert 0.35 * total <= sum(ds) <= 2.2 * total, \
            f"sum {sum(ds):.1f} drifted too far from {total}"


def test_durations_bell_shape():
    rng = random.Random(5)
    ds = H.durations(1000, 21, rng, jitter=0.0)
    mid, ends = ds[10], ds[0] + ds[-1]
    assert mid > ends, "profile is not bell-shaped (middle should be slowest)"


def test_durations_zero_and_edge():
    rng = random.Random(2)
    assert H.durations(0, 5, rng) == [0.0] * 5
    assert len(H.durations(100, 0, rng)) == 1  # steps clamped to >= 1


# --------------------------------------------------------------------------
# char_delay / move_duration / scroll_plan
# --------------------------------------------------------------------------

def test_char_delay_distribution():
    """A bounds assertion here would only restate the function's own clamp, so
    check the shape it is supposed to have instead."""
    rng = random.Random(9)
    letters = sorted(H.char_delay("a", rng) for _ in range(400))
    assert len(set(letters)) > 50, "delays are constant — no log-normal spread"
    median = letters[len(letters) // 2]
    assert 0.03 < median < 0.25, f"median letter delay {median:.3f}s is not ~85ms"
    assert letters[0] < letters[-1] * 0.5, "no long tail of hesitations"

    spaces = [H.char_delay(" ", rng) for _ in range(400)]
    assert sum(spaces) > sum(letters), \
        "a space should pause longer than an average letter"


def test_move_duration_monotonic_and_bounded():
    ds = [H.move_duration(d) for d in (0, 50, 200, 800, 2000)]
    assert ds == sorted(ds), "duration must grow with distance"
    assert ds[0] >= 40, "floor is missing"
    assert 150 < H.move_duration(500) < 900, H.move_duration(500)


def test_scroll_plan_sums_to_target():
    rng = random.Random(13)
    for px in (800, -400, 2500, 50):
        plan = H.scroll_plan(px, rng)
        assert plan, f"no plan for {px}"
        total = sum(d for d, _ in plan)
        assert abs(total - px) <= abs(px) * 0.30, \
            f"scroll {px} planned for {total}"
        assert all(p >= 0.020 for _, p in plan), "pause below floor"
        assert all(d != 0 for d, _ in plan), "zero-delta wheel event"


def test_scroll_plan_tiny():
    rng = random.Random(4)
    assert H.scroll_plan(0, rng) == []
    assert H.scroll_plan(0.4, rng) == []


# --------------------------------------------------------------------------
# Human against the FakePage
# --------------------------------------------------------------------------

def test_move_reaches_target_and_is_sampled():
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=1)
    h.move(400, 300)
    moves = [e for e in log if e[0] == "move"]
    assert len(moves) > 1, "mouse teleported instead of travelling"
    assert moves[-1][1:] == (400.0, 300.0), f"ended at {moves[-1]}"
    assert h.pos == (400.0, 300.0)


def test_move_points_and_durations_are_zipped_1to1():
    """zip() truncates silently, so a points/durations mismatch would drop the
    tail of the path and stop short of the target without any error."""
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=2)
    real = H.durations
    seen = {}

    def spy(total, steps, rng, **kw):
        seen["steps"] = steps
        return real(total, steps, rng, **kw)

    H.durations = spy
    try:
        for target in [(10, 10), (900, 700), (300, 20)]:
            log.clear()
            h.move(*target)
            moves = [e for e in log if e[0] == "move"]
            assert moves, f"move to {target} dispatched nothing"
            assert len(moves) == seen["steps"], \
                f"{len(moves)} moves for {seen['steps']} durations"
            assert h.pos == (float(target[0]), float(target[1]))
    finally:
        H.durations = real


def test_click_presses_and_releases():
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=3)
    h.click("#go")
    kinds = [e[0] for e in log]
    assert "down" in kinds and "up" in kinds, "no press/release"
    assert kinds.index("down") < kinds.index("up")


def test_click_point_is_inside_the_element_and_varies():
    """The point must stay inside the box *and* move around: aiming dead centre
    every time is exactly the tell the gaussian offset exists to remove."""
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=4)
    points = []
    for _ in range(25):
        log.clear()
        h.click("#go")
        # Position at the moment of the press — click_at may wander afterwards.
        down = next(i for i, e in enumerate(log) if e[0] == "down")
        _, x, y = [e for e in log[:down] if e[0] == "move"][-1]
        assert 100 <= x <= 220 and 100 <= y <= 140, f"clicked outside: {x},{y}"
        points.append((x, y))
    assert len(set(points)) > 20, "every click landed on the same pixel"
    assert (160.0, 120.0) not in points, "clicked dead centre of the box"


def test_clear_field_clicks_where_it_is_told():
    """The clear must use the field's point, not the tracked cursor.

    click_at() drifts the cursor off the element after clicking some of the
    time. Clearing at the drifted position triple-clicks whatever is there
    instead, which blurs the field — so the following keystrokes go nowhere and
    the old value survives, with no error anywhere.
    """
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=4)
    h.pos = (900.0, 900.0)          # cursor drifted far away from the field
    h._clear_field((160.0, 120.0))
    clicks = [e for e in log if e[0] == "click"]
    assert clicks, "clearing did not click at all"
    assert clicks[0][1] == 3, f"expected a triple-click, got {clicks[0][1]}"
    assert clicks[0][2:] == (160.0, 120.0), f"cleared at {clicks[0][2:]}"


def test_type_into_clears_inside_the_field():
    """Many draws, so the drift branch is exercised: every clear still has to
    land inside the box the field occupies."""
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=4)
    for _ in range(40):
        log.clear()
        h.type_into("#go", "abc", clear=True, typo_rate=0.0)
        triples = [e for e in log if e[0] == "click" and e[1] == 3]
        assert triples, "clearing never triple-clicked"
        _, _, x, y = triples[0]
        assert 100 <= x <= 220 and 100 <= y <= 140, f"clear clicked at {x},{y}"


def test_typing_produces_every_character_in_order():
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=5)
    h.type_text("hello world", typo_rate=0.0)
    typed = "".join(e[1] for e in log if e[0] == "type")
    assert typed == "hello world", repr(typed)
    assert not [e for e in log if e[0] == "press" and e[1] == "Backspace"], \
        "a typo was made even with typo_rate=0"


def test_typos_are_always_corrected():
    """With typos forced, every wrong character must be backspaced away, so the
    net text on screen is still correct."""
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=6)
    h.type_text("correction test", typo_rate=1.0)

    screen = []
    for e in log:
        if e[0] == "type":
            screen.append(e[1])
        elif e[0] == "press" and e[1] == "Backspace":
            assert screen, "backspace with nothing typed"
            screen.pop()
    assert "".join(screen) == "correction test", repr("".join(screen))
    assert [e for e in log if e[0] == "press" and e[1] == "Backspace"], \
        "typo_rate=1.0 produced no typos at all"


def test_typo_char_is_a_neighbour_never_the_same_key():
    rng = random.Random(8)
    for ch in "abcdefghijklmnopqrstuvwxyz":
        wrong = H.typo_char(ch, rng)
        assert wrong != ch, f"{ch} -> {wrong} is not a typo"
        assert wrong in H.QWERTY_NEIGHBOURS[ch], f"{wrong} is not adjacent to {ch}"


def test_level_off_dispatches_single_move():
    page, log = fake_page()
    h = H.Human(page, level="off", seed=1)
    assert h.speed == 0.0
    h.move(500, 400)
    assert len([e for e in log if e[0] == "move"]) == 1, "off should not interpolate"


def test_speed_is_reconfigurable_per_command():
    """daemon.py mutates .speed between requests; off must not stick."""
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=1)
    h.speed = 0.0
    h.speed = H.LEVELS["normal"]
    log.clear()
    h.move(600, 500)
    assert len([e for e in log if e[0] == "move"]) > 1, "speed stayed at off"


def test_missing_selector_raises_lookup_error():
    page, log = fake_page(els=0)
    h = H.Human(page, level="normal", seed=1)
    try:
        h.click("#nope")
    except LookupError:
        return
    raise AssertionError("missing selector did not raise LookupError")


def test_scroll_delegates_to_wheel_events():
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=1)
    h.scroll(900)
    wheeled = sum(e[1] for e in log if e[0] == "wheel")
    assert abs(wheeled - 900) <= 900 * 0.35, f"wheeled {wheeled} for a 900px scroll"


def test_scroll_up_is_negative():
    page, log = fake_page()
    h = H.Human(page, level="normal", seed=1)
    h.scroll(500, up=True)
    wheeled = sum(e[1] for e in log if e[0] == "wheel")
    assert wheeled < 0, f"scroll(up=True) produced {wheeled}"


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            failed.append(name)
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
