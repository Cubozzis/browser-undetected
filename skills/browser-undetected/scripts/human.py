#!/usr/bin/env python3
"""Human-like input primitives for Patchright.

Everything here exists so that synthetic input stops looking synthetic: curved
mouse paths with hand tremor, Fitts's-law timing, log-normal typing cadence,
realistic typos that get corrected, eased scrolling and idle micro-movements.

The pure-math helpers (``path``, ``durations``, ``char_delay``, ``scroll_plan``,
``move_duration``) never touch a browser and are covered by
``tests/test_human.py``. :class:`Human` binds them to a Playwright ``Page``.

Every source of randomness goes through one seeded ``random.Random`` so a run
can be replayed with ``--seed`` when you are debugging a flaky flow.
"""
from __future__ import annotations

import math
import random
import sys
import time

__all__ = [
    "Human",
    "path",
    "durations",
    "char_delay",
    "scroll_plan",
    "move_duration",
    "LEVELS",
]

#: Select-all modifier. On macOS "Control+A" is not select-all, so the raw
#: (non-human) clear path would leave the old value in place and typing would
#: append to it. Public because daemon.py needs the same key for its in-frame
#: clear, which cannot go through Human.
SELECT_ALL_MOD = "Meta" if sys.platform == "darwin" else "Control"

# --------------------------------------------------------------------------
# tuning
# --------------------------------------------------------------------------

#: Speed multipliers per humanisation level. ``off`` degrades to raw Playwright.
LEVELS = {"off": 0.0, "fast": 0.55, "normal": 1.0, "careful": 1.7}

#: Physical QWERTY neighbours, used to pick *plausible* typos (fat-finger on an
#: adjacent key) instead of random garbage that no human would ever produce.
QWERTY_NEIGHBOURS = {
    "q": "was", "w": "qeasd", "e": "wrsdf", "r": "etdfg", "t": "ryfgh",
    "y": "tughj", "u": "yihjk", "i": "uojkl", "o": "ipkl", "p": "ol",
    "a": "qwszx", "s": "qweadzxc", "d": "wersfxcv", "f": "ertdgcvb",
    "g": "rtyfhvbn", "h": "tyugjbnm", "j": "yuihknm", "k": "uiojlm", "l": "iopk",
    "z": "asx", "x": "asdzc", "c": "sdfxv", "v": "dfgcb", "b": "fghvn",
    "n": "ghjbm", "m": "hjkn",
}

#: Characters that make a typist pause afterwards.
_SLOW_CHARS = ",.;:!?)]}"
#: Pause before a character (thinking / reach).
_PRE_SLOW = "([{"


# --------------------------------------------------------------------------
# pure math
# --------------------------------------------------------------------------

def _cubic(p0, c1, c2, p1, t):
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0],
            a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1])


def move_duration(distance, level=1.0):
    """Milliseconds a human needs to travel ``distance`` px (Fitts's law).

    Roughly ``a + b * log2(dist / target + 1)``; the constants are eyeballed
    against real pointer logs: ~260 ms for a 100 px flick, ~570 ms for 500 px.
    """
    dist = max(0.0, float(distance))
    ms = 90.0 + 220.0 * math.log2(dist / 140.0 + 1.0)
    return max(40.0, ms * max(0.05, level))


def path(x0, y0, x1, y1, rng, strength=1.0, steps=None, tremor=True):
    """Cubic-Bézier mouse path from ``(x0, y0)`` to ``(x1, y1)``.

    Control points are pushed perpendicular to the straight line so the curve
    bows like a real arm, then a small gaussian tremor is added per sample.
    Returns a list of ``(x, y)`` points *excluding* the start.
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 2.0:
        return [(float(x1), float(y1))]

    if steps is None:
        steps = int(min(64, max(8, dist / 16.0)))
    steps = max(2, int(steps))

    # Unit normal to the travel vector.
    nx, ny = -dy / dist, dx / dist
    bow = min(140.0, dist * 0.32) * strength
    c1 = (x0 + dx * 0.25 + nx * rng.gauss(0, bow),
          y0 + dy * 0.25 + ny * rng.gauss(0, bow))
    c2 = (x0 + dx * 0.75 + nx * rng.gauss(0, bow),
          y0 + dy * 0.75 + ny * rng.gauss(0, bow))

    pts = []
    for i in range(1, steps + 1):
        t = i / float(steps)
        px, py = _cubic((x0, y0), c1, c2, (x1, y1), t)
        if tremor:
            # Tremor scales down as the hand settles on target.
            amp = 1.1 * (1.0 - t) + 0.15
            px += rng.gauss(0, amp)
            py += rng.gauss(0, amp)
        pts.append((px, py))
    pts[-1] = (float(x1), float(y1))
    return pts


def durations(total_ms, steps, rng, jitter=0.30, floor_ms=4.0):
    """Split ``total_ms`` over ``steps`` with a bell-shaped velocity profile.

    Humans accelerate then decelerate, so the middle samples get more time than
    the ends. Each step is jittered so the profile is never machine-regular.
    """
    total_ms = max(0.0, float(total_ms))
    steps = max(1, int(steps))
    if total_ms <= 0.0:
        return [0.0] * steps

    weights = [math.sin(math.pi * (i + 0.5) / steps) for i in range(steps)]
    wsum = sum(weights) or 1.0
    out = []
    for w in weights:
        d = total_ms * (w / wsum) * (1.0 + rng.gauss(0, jitter))
        out.append(max(floor_ms, d))
    return out


def char_delay(ch, rng, base=0.085, sd=0.38):
    """Seconds to wait before/after typing ``ch``.

    Log-normal, because inter-key intervals are: mostly ~60-120 ms with a long
    right tail for hesitations. Spaces and punctuation get extra pause.
    """
    d = rng.lognormvariate(math.log(base), sd)
    if ch == " ":
        d *= rng.uniform(1.3, 2.3)
    elif ch in _SLOW_CHARS:
        d *= rng.uniform(1.4, 2.7)
    elif ch in _PRE_SLOW:
        d *= rng.uniform(1.2, 2.0)
    elif ch.isupper():
        d *= rng.uniform(1.15, 1.55)
    return max(0.012, min(d, 0.95))


def scroll_plan(px, rng):
    """Turn one "scroll by N px" into a burst of wheel events with pauses.

    Real wheels do not emit a single 800 px jump; they emit 3-8 notches of
    varying size with 30-150 ms gaps, and the burst eases in and out.
    """
    px = float(px)
    if abs(px) < 1:
        return []
    n = max(2, min(9, int(abs(px) / 110.0) + rng.randint(1, 3)))
    weights = [math.sin(math.pi * (i + 0.5) / n) for i in range(n)]
    wsum = sum(weights) or 1.0
    out = []
    for w in weights:
        delta = px * (w / wsum) * rng.uniform(0.75, 1.25)
        pause = max(0.020, rng.gauss(0.070, 0.035))
        out.append((int(round(delta)), pause))
    return out


def typo_char(ch, rng):
    """A plausible wrong key for ``ch`` (adjacent key, shifted variant, or swap)."""
    low = ch.lower()
    pool = QWERTY_NEIGHBOURS.get(low)
    if pool:
        wrong = rng.choice(pool)
    else:
        # Non-letter: hit the key next to it on the number row, or repeat it.
        wrong = rng.choice([ch, "1", "2", "3"]) if ch.isdigit() else ch
    if ch.isupper():
        wrong = wrong.upper()
    return wrong


# --------------------------------------------------------------------------
# Human
# --------------------------------------------------------------------------

class Human:
    """Binds human-like input to a Playwright ``Page``.

    The page's own ``mouse`` position is unknown to Playwright, so this class
    tracks a virtual cursor in ``self.pos`` and always moves from there — that
    continuity is most of what makes a pointer trace look non-synthetic.
    """

    def __init__(self, page, level="normal", seed=None, rng=None):
        self.page = page
        self.level_name = level
        self.rng = rng or random.Random(seed)
        self.speed = LEVELS.get(level, 1.0)
        self.pos = (float(self.rng.uniform(120, 700)),
                    float(self.rng.uniform(80, 400)))

    # -- internals ---------------------------------------------------------

    @property
    def _human(self):
        return self.speed > 0.0

    def _sleep(self, seconds):
        if seconds > 0:
            time.sleep(seconds)

    def _el(self, selector, nth=0):
        els = self.page.query_selector_all(selector)
        if not els:
            raise LookupError(f"selector matched nothing: {selector!r}")
        idx = nth if nth >= 0 else len(els) + nth
        try:
            return els[idx]
        except IndexError:
            raise LookupError(
                f"selector {selector!r} matched {len(els)} element(s), nth={nth}") from None

    def _point_in(self, el):
        """Pick a click point inside ``el`` — centre-ish, never dead centre."""
        try:
            el.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        box = el.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise LookupError("element has no visible box")
        fx = min(0.78, max(0.22, 0.5 + self.rng.gauss(0, 0.11)))
        fy = min(0.78, max(0.22, 0.5 + self.rng.gauss(0, 0.11)))
        x = box["x"] + box["width"] * fx
        y = box["y"] + box["height"] * fy
        if box["width"] > 6:
            x = min(box["x"] + box["width"] - 2, max(box["x"] + 2, x))
        if box["height"] > 6:
            y = min(box["y"] + box["height"] - 2, max(box["y"] + 2, y))
        return x, y

    # -- mouse -------------------------------------------------------------

    def move(self, x, y, strength=1.0, settle=True):
        """Move the virtual cursor to ``(x, y)`` along a bowed path."""
        x, y = float(x), float(y)
        if not self._human:
            self.page.mouse.move(x, y)
            self.pos = (x, y)
            return
        x0, y0 = self.pos
        pts = path(x0, y0, x, y, self.rng, strength=strength)
        total = move_duration(math.hypot(x - x0, y - y0), self.speed)
        for (px, py), d in zip(pts, durations(total, len(pts), self.rng)):
            self.page.mouse.move(px, py)
            self._sleep(d / 1000.0)
        self.pos = (x, y)
        if settle:
            self._sleep(self.rng.uniform(0.02, 0.09))

    def _wander(self, radius=26.0, moves=2):
        """A few aimless micro-moves — what a hand does while thinking."""
        for _ in range(max(1, moves)):
            x, y = self.pos
            self.move(x + self.rng.gauss(0, radius),
                      y + self.rng.gauss(0, radius),
                      strength=0.45, settle=False)
            self._sleep(self.rng.uniform(0.05, 0.28))

    def click_at(self, x, y, button="left", clicks=1):
        self.move(x, y)
        if self._human:
            self._sleep(self.rng.uniform(0.05, 0.18))  # hover before commit
        for _ in range(max(1, clicks)):
            self.page.mouse.down(button=button)
            if self._human:
                self._sleep(self.rng.uniform(0.045, 0.135))
            self.page.mouse.up(button=button)
            if clicks > 1:
                self._sleep(self.rng.uniform(0.04, 0.11))
        if self._human and self.rng.random() < 0.12:
            self._wander(radius=14.0, moves=1)  # drift off after clicking

    def click(self, selector, nth=0, button="left", clicks=1, timeout=None):
        el = self._el(selector, nth)
        x, y = self._point_in(el)
        self.click_at(x, y, button=button, clicks=clicks)
        return el

    def hover(self, selector, nth=0):
        el = self._el(selector, nth)
        x, y = self._point_in(el)
        self.move(x, y)
        if self._human:
            self._sleep(self.rng.uniform(0.25, 0.8))
        return el

    def drag(self, selector_from, selector_to, nth_from=0, nth_to=0):
        a = self._el(selector_from, nth_from)
        b = self._el(selector_to, nth_to)
        x1, y1 = self._point_in(a)
        x2, y2 = self._point_in(b)
        # Approach and press without releasing: a full click_at() here would
        # fire the source element's click handler before the drag starts.
        self.move(x1, y1)
        self._sleep(self.rng.uniform(0.05, 0.18))
        self.page.mouse.down()
        self._sleep(self.rng.uniform(0.08, 0.2))
        self.move(x2, y2, strength=0.6)
        self._sleep(self.rng.uniform(0.08, 0.25))
        self.page.mouse.up()

    # -- keyboard ----------------------------------------------------------

    def press(self, key, repeat=1):
        for _ in range(max(1, repeat)):
            self.page.keyboard.press(key)
            if self._human:
                self._sleep(self.rng.uniform(0.04, 0.13))

    def type_text(self, text, clear=False, submit=False, typo_rate=0.02, at=None):
        """Type ``text`` one character at a time with human cadence and typos.

        ``typo_rate`` is the per-character chance of a fat-finger: type a
        neighbouring key, notice it, backspace, retype. Set 0 for zero risk.

        ``at`` is the (x, y) of the field being typed into, when there is one.
        Clearing needs it: see :meth:`_clear_field`.
        """
        if clear:
            self._clear_field(at)
        if not self._human:
            if text:
                self.page.keyboard.type(text)
            if submit:
                self.page.keyboard.press("Enter")
            return

        i = 0
        while i < len(text):
            ch = text[i]
            self._sleep(char_delay(ch, self.rng))

            if typo_rate > 0 and ch not in " \n\t" and self.rng.random() < typo_rate:
                wrong = typo_char(ch, self.rng)
                self.page.keyboard.type(wrong)
                self._sleep(self.rng.uniform(0.09, 0.34))  # notice it
                self.page.keyboard.press("Backspace")
                self._sleep(self.rng.uniform(0.06, 0.22))
                self.page.keyboard.type(ch)
            else:
                self.page.keyboard.type(ch)

            # Occasional mid-word hesitation — much more human than a metronome.
            if ch == " " and self.rng.random() < 0.10:
                self._sleep(self.rng.uniform(0.25, 0.8))
            i += 1

        if submit:
            self._sleep(self.rng.uniform(0.15, 0.5))
            self.page.keyboard.press("Enter")

    def _clear_field(self, at=None):
        if not self._human:
            self.page.keyboard.press(f"{SELECT_ALL_MOD}+A")
            self.page.keyboard.press("Delete")
            return
        # Triple-click selects the field's contents; then one deliberate Delete.
        # Not at self.pos: click_at() drifts the cursor off the element after
        # clicking some of the time, and a triple-click at the drifted position
        # lands on whatever is there instead — which blurs the field, so the
        # text that follows goes nowhere and the old value survives.
        x, y = at if at else self.pos
        self.page.mouse.click(x, y, click_count=3)
        self.pos = (float(x), float(y))
        self._sleep(self.rng.uniform(0.08, 0.2))
        self.page.keyboard.press("Delete")
        self._sleep(self.rng.uniform(0.05, 0.15))

    def type_into(self, selector, text, nth=0, clear=True, submit=False,
                  typo_rate=0.02):
        el = self._el(selector, nth)
        x, y = self._point_in(el)
        self.click_at(x, y)
        self._sleep(self.rng.uniform(0.08, 0.3))  # let focus land
        self.type_text(text, clear=clear, submit=submit, typo_rate=typo_rate,
                       at=(x, y))

    # -- scroll / idle -----------------------------------------------------

    def scroll(self, px, up=False):
        """Wheel-scroll ``px`` pixels (negative or ``up=True`` scrolls back)."""
        px = -abs(px) if up else px
        if not self._human:
            self.page.mouse.wheel(0, int(px))
            return
        for delta, pause in scroll_plan(px, self.rng):
            self.page.mouse.wheel(0, delta)
            self._sleep(pause)
        # Reading drift: a small step back the way we came, to re-read a line.
        # Signed against px, so an upward scroll drifts back down rather than
        # continuing further up.
        if px and self.rng.random() < 0.18:
            self._sleep(self.rng.uniform(0.3, 0.9))
            drift = abs(px) * self.rng.uniform(0.06, 0.18)
            self.page.mouse.wheel(0, -int(math.copysign(drift, px)))

    def scroll_to(self, selector, nth=0):
        el = self._el(selector, nth)
        try:
            el.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        if self._human:
            self._sleep(self.rng.uniform(0.2, 0.6))

    def idle(self, seconds):
        """Look alive for ``seconds``: micro-moves, random pauses, tiny scrolls."""
        if not self._human:
            self._sleep(seconds)
            return
        end = time.time() + max(0.0, float(seconds))
        while time.time() < end:
            self._wander(radius=self.rng.uniform(8, 40),
                         moves=self.rng.randint(1, 3))
            if self.rng.random() < 0.22:
                self.page.mouse.wheel(0, int(self.rng.gauss(0, 60)))
            self._sleep(min(self.rng.uniform(0.2, 1.1),
                            max(0.0, end - time.time())))

    def settle(self):
        """Short beat before reading a value or firing the next command."""
        if self._human:
            self._sleep(self.rng.uniform(0.15, 0.5))
