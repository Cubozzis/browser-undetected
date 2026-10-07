#!/usr/bin/env python3
"""Persistent Patchright browser daemon.

Holds one stealth browser alive between CLI invocations and exposes it over
HTTP on 127.0.0.1, so any agent that can run a shell command (Claude Code,
Codex, Aider, a Makefile, you) can drive a real browser across many separate
processes without losing cookies, tabs or page state.

Started by ``bu.py`` — you normally never run this directly.

Configuration is entirely through environment variables (see ``bu.py``), all
prefixed ``BU_``.

**Threading model.** Playwright's sync API belongs to the thread that created
it, so exactly one thread — the worker below — ever touches the browser. HTTP
request threads do nothing but validate the request and hand the command to
that worker over a queue, which is what keeps a slow command from blocking the
liveness probe the CLI depends on.
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from human import Human, SELECT_ALL_MOD  # noqa: E402

from patchright.sync_api import sync_playwright  # noqa: E402

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def env(name, default=None):
    v = os.environ.get("BU_" + name)
    return default if v is None or v == "" else v

PORT = int(env("PORT", "9317"))
#: Required on every request. Generated when not supplied rather than left
#: empty: the daemon holds a real, often logged-in browser and `/eval` runs
#: arbitrary JS in it, so "no token configured" must never mean "no auth".
TOKEN = env("TOKEN") or secrets.token_hex(16)
HEADLESS = env("HEADLESS", "0") == "1"
CHANNEL = env("CHANNEL", "")          # "chrome" | "chromium" | "msedge" | ""
PROFILE = env("PROFILE", "default")
HOME = env("HOME", os.path.join(os.path.expanduser("~"), ".browser-undetected"))
LEVEL = env("LEVEL", "normal")
SEED = env("SEED", "")
PROXY = env("PROXY", "")
BLOCK = env("BLOCK", "")
LOCALE = env("LOCALE", "")
TIMEZONE = env("TIMEZONE", "")
CAPTURE_BODIES = env("CAPTURE_BODIES", "0") == "1"
DIALOG_MODE = env("DIALOG", "accept")  # accept | dismiss
MAX_BODY = int(env("MAX_BODY", "20000"))
WIN = env("WINDOW", "1366x850")

try:
    WIN_W, WIN_H = (int(x) for x in WIN.lower().split("x", 1))
except Exception:
    WIN_W, WIN_H = 1366, 850

PROFILE_DIR = os.path.join(HOME, "profiles", PROFILE)

# --------------------------------------------------------------------------
# network capture
# --------------------------------------------------------------------------

recent = deque(maxlen=5000)
ws_recent = deque(maxlen=2000)
_file_handles: dict[str, object] = {}
_shot_n: dict[str, int] = {}
_last_dialog = {"message": "", "type": ""}
_attached: set[int] = set()
_PAGE_WD: dict[int, str] = {}   # page identity -> workdir that navigated it


def _art(wd):
    d = os.path.join(wd, ".browser")
    os.makedirs(d, exist_ok=True)
    return d


def _sink(wd):
    """Append-only JSONL handle per working directory (so projects don't mix).

    Capped: a daemon can outlive a lot of `cd`s, and a live fd per directory
    forever is a leak. The oldest handle is closed on eviction.
    """
    try:
        h = _file_handles.get(wd)
        if h is None or h.closed:
            if len(_file_handles) >= 32:
                old, oh = next(iter(_file_handles.items()))
                try:
                    oh.close()
                except Exception:
                    pass
                _file_handles.pop(old, None)
            h = open(os.path.join(_art(wd), "requests.jsonl"), "a",
                     buffering=1, encoding="utf-8")
            _file_handles[wd] = h
        return h
    except Exception:
        return None


def log_event(ev, wd):
    recent.append(ev)
    h = _sink(wd)
    if h:
        try:
            h.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _wd():
    """Working directory of the *current* request (set per request)."""
    return _CURRENT_WD[0] or os.getcwd()


def _safe_wd(value):
    """Accept a client workdir only if it names an existing directory.

    It is used as a path prefix for every artifact this daemon writes, so it
    must not be a relative name, a file, or something that only exists after we
    create it.
    """
    if not value:
        return None
    try:
        path = os.path.abspath(os.path.expanduser(str(value)))
    except Exception:
        return None
    return path if os.path.isdir(path) else None


def _abs(path, wd=None):
    """Resolve a client-supplied path against the *caller's* directory.

    The daemon's own cwd is frozen where `bu start` was run, so resolving a
    relative path here silently reads and writes files in the wrong project —
    a `bu screenshot out.png` from another directory lands in the old one, and
    `bu addcookies cookies.json` can load a same-named file from there.
    """
    path = os.path.expanduser(str(path))
    return path if os.path.isabs(path) else os.path.join(wd or _wd(), path)


_CURRENT_WD = [None]


def _remember_page_wd(page):
    """Pin a page to the directory that navigated it (see _wd_for)."""
    if len(_PAGE_WD) >= 64:   # ids of closed pages would otherwise pile up
        _PAGE_WD.pop(next(iter(_PAGE_WD)), None)
    _PAGE_WD[id(page)] = _wd()


def _wd_for(request):
    """Which directory's log a network event belongs to.

    Network events also arrive *between* requests (background XHR, a page still
    polling), when _CURRENT_WD holds whatever directory ran last. Preferring the
    directory that navigated the originating page keeps async traffic filed
    under the project that started it.
    """
    try:
        return _PAGE_WD.get(id(request.frame.page)) or _wd()
    except Exception:
        return _wd()


def on_request(r):
    try:
        log_event({"t": "req", "time": time.time(), "method": r.method, "url": r.url,
                   "type": r.resource_type, "headers": dict(r.headers),
                   "post": (r.post_data or "")[:4000]}, _wd_for(r))
    except Exception:
        pass


def on_response(r):
    try:
        ev = {"t": "res", "time": time.time(), "status": r.status, "url": r.url,
              "type": r.request.resource_type}
        if CAPTURE_BODIES and r.request.resource_type in ("xhr", "fetch", "document"):
            try:
                ct = (r.headers or {}).get("content-type", "")
                if "json" in ct or "text" in ct:
                    body = r.text()
                    if body and len(body) < MAX_BODY:
                        ev["body"] = body
            except Exception:
                pass
        log_event(ev, _wd_for(r.request))
    except Exception:
        pass


def _wsframe(ws, ev, payload):
    try:
        data = payload if isinstance(payload, str) else f"[binary {len(payload)}B]"
        frame = {"t": "ws", "ev": ev, "time": time.time(), "url": ws.url,
                 "data": data[:4000]}
        ws_recent.append(frame)
        log_event(frame, _wd())
    except Exception:
        pass


def on_ws(ws):
    log_event({"t": "ws", "ev": "open", "time": time.time(), "url": ws.url}, _wd())
    ws.on("framesent", lambda p: _wsframe(ws, "send", p))
    ws.on("framereceived", lambda p: _wsframe(ws, "recv", p))
    ws.on("close", lambda: log_event({"t": "ws", "ev": "close",
                                      "time": time.time(), "url": ws.url}, _wd()))


def _attach_page(pg):
    # Both the context's "page" event and newtab() call this; without the guard
    # every new tab gets its WebSocket and dialog handlers registered twice.
    if id(pg) in _attached:
        return
    _attached.add(id(pg))
    pg.on("websocket", on_ws)
    pg.on("dialog", on_dialog)


def on_dialog(d):
    _last_dialog["message"] = d.message
    _last_dialog["type"] = d.type
    try:
        d.accept() if DIALOG_MODE == "accept" else d.dismiss()
    except Exception:
        pass


# --------------------------------------------------------------------------
# browser boot
# --------------------------------------------------------------------------

def _proxy_conf():
    from urllib.parse import unquote, urlparse as _up
    u = _up(PROXY)
    conf = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        conf["username"] = unquote(u.username)
        conf["password"] = unquote(u.password)
    return conf


def _has_gpu():
    """True when the box exposes a real GPU (so hardware WebGL is available).

    Easy to answer on Linux and only interesting there: a headless VPS or a
    container has no /dev/dri, a desktop does. macOS and Windows machines all
    have one, and the fallback flag is a no-op when a GPU is present anyway.
    """
    if sys.platform != "linux":
        return True
    try:
        return bool(os.listdir("/dev/dri"))
    except OSError:
        return False


def _launch_kwargs():
    # Deliberately tiny. Patchright rewrites Chromium's switch list itself:
    # it ADDS --disable-blink-features=AutomationControlled, REMOVES
    # --enable-automation, --disable-extensions, --disable-popup-blocking,
    # --disable-component-update and --disable-default-apps. Re-adding any of
    # those by hand (or overriding ignore_default_args, which would put
    # --disable-extensions back) undoes the patch. Playwright also already
    # passes --disable-dev-shm-usage and --no-first-run.
    args = [f"--window-size={WIN_W},{WIN_H}"]
    # Chrome 137+ refuses the software WebGL fallback unless this is passed, so
    # a GPU-less box (any VPS, most containers) serves *no WebGL at all* — and
    # "getContext('webgl') === null" is one of the loudest headless tells there
    # is, on top of breaking every WebGL-using site. The flag only *permits*
    # SwiftShader; where a real GPU exists Chrome still renders in hardware.
    if not _has_gpu():
        args.append("--enable-unsafe-swiftshader")
    if PROXY:
        # Without this Chromium leaks the real IP over WebRTC STUN even when
        # every HTTP request goes through the proxy.
        args.append("--webrtc-ip-handling-policy=disable_non_proxied_udp")
    kw = {
        "user_data_dir": PROFILE_DIR,
        "headless": HEADLESS,
        # no_viewport keeps the viewport glued to the real window size; a fixed
        # viewport that disagrees with the window is a classic fingerprint.
        "no_viewport": True,
        "accept_downloads": True,
        "args": args,
    }
    if CHANNEL:
        kw["channel"] = CHANNEL
    if PROXY:
        kw["proxy"] = _proxy_conf()
    if LOCALE:
        kw["locale"] = LOCALE
    if TIMEZONE:
        kw["timezone_id"] = TIMEZONE
    return kw


#: Chrome refusing to start because it thinks the profile is still owned.
_PROFILE_BUSY = ("existing browser session", "already in use",
                 "ProcessSingleton", "SingletonLock")

#: The requested channel is not installed on this machine.
_CHANNEL_MISSING = ("executable doesn't exist", "is not found",
                    "no such file or directory")


def _channel_missing(e):
    msg = str(e).lower()
    return any(s in msg for s in _CHANNEL_MISSING)


def _launch_with_retry(pw, kw, tries=8, delay=1.5):
    """Launch, riding out a profile that a dying Chrome has not released yet.

    The usual cause is our own previous instance: ctx.close() returns before the
    OS has reaped Chrome, and for a second or two the ProcessSingleton is still
    held. Retrying beats failing a whole restart on a one-second race.
    """
    last = None
    for i in range(tries):
        try:
            return pw.chromium.launch_persistent_context(**kw)
        except Exception as e:
            last = e
            if not any(s.lower() in str(e).lower() for s in _PROFILE_BUSY):
                raise
            if i == tries - 1:
                break
            print(f"[bu] profile busy, retrying in {delay}s ({i + 1}/{tries})",
                  flush=True)
            time.sleep(delay)
    raise last


def boot():
    global pw, ctx, human, CHANNEL
    pw = sync_playwright().start()
    last = None
    for attempt in (CHANNEL, "") if CHANNEL else ("",):
        try:
            kw = _launch_kwargs()
            if not attempt:
                kw.pop("channel", None)
            ctx = _launch_with_retry(pw, kw)
            # Record what actually launched. The fallback below means the
            # channel that was asked for is not always the channel running,
            # and `bu doctor` / `bu status` are the only place a user can see
            # the difference.
            CHANNEL = attempt
            break
        except Exception as e:
            last = e
            # Only fall back to the bundled browser when the *channel* is
            # missing. Falling back on any error masks the real one behind a
            # second, unrelated failure — which is how a profile lock ends up
            # reported as a missing Chromium download.
            if not attempt or not _channel_missing(e):
                raise
            print(f"[bu] channel={attempt!r} is not installed ({e}); "
                  f"falling back to the bundled browser", flush=True)
    else:
        raise last

    ctx.on("request", on_request)
    ctx.on("response", on_response)
    ctx.on("page", _attach_page)
    for pg in ctx.pages:
        _attach_page(pg)

    if BLOCK:
        ctx.route(re.compile(BLOCK), lambda route: route.abort())

    human = Human(_page(), level=LEVEL, seed=SEED or None)
    return ctx


def _page(create=True):
    """The page commands act on: the one `bu tab` selected, else the newest.

    Playwright's context.pages order is not tab-strip order, so "the last one"
    is only a default — an explicit selection is what `bu tab` has to pin down,
    otherwise `tab 0` would activate a tab and every later command would ignore
    it and keep talking to the newest one.
    """
    idx = PAGE[0]
    if idx is not None:
        try:
            return ctx.pages[idx]
        except IndexError:
            PAGE[0] = None
    if ctx.pages:
        return ctx.pages[-1]
    return ctx.new_page() if create else None


PAGE = [None]   # index of the tab the caller selected, or None for "newest"
FRAME = [None]


def cur():
    """The object selectors resolve against: the frame if one is selected.

    A frame goes stale the moment it navigates or its page changes, and every
    later command would then fail with "frame was detached". Touching .url is
    how we find out, and falling back to the page is the graceful recovery.
    """
    f = FRAME[0]
    if f is not None:
        try:
            f.url
            return f
        except Exception:
            FRAME[0] = None
    return _page()


# --------------------------------------------------------------------------
# command handling
# --------------------------------------------------------------------------

#: Commands that must not conjure a tab just to answer — asking for the request
#: log should not silently open a blank page and change the tab count.
NO_PAGE_CMDS = {"ping", "doctor", "requests", "ws", "clear", "tabs", "close"}


def handle(cmd, q):
    global human, DIALOG_MODE
    a = lambda k, d=None: (q.get(k, [d])[0] if k in q else d)
    page = _page(create=cmd not in NO_PAGE_CMDS)

    # keep the human cursor attached to whatever page is on top
    if human.page is not page:
        human = Human(page, level=LEVEL, seed=SEED or None, rng=human.rng)
    h = human
    # Recompute per request: the Human object outlives a single call, so a
    # `raw=1` on one command must not leave every later command raw too.
    from human import LEVELS
    h.speed = LEVELS.get(a("level") or LEVEL, 1.0)
    raw = a("raw", "0") == "1"
    if raw:
        h.speed = 0.0

    # ---- lifecycle -------------------------------------------------------
    if cmd == "ping":
        return {"ok": True, "url": page.url if page else "",
                "title": _safe_title(page) if page else "",
                "headless": HEADLESS, "channel": CHANNEL or "chromium",
                "profile": PROFILE, "level": h.level_name,
                "tab": PAGE[0], "tabs": len(ctx.pages)}
    if cmd == "close":
        return {"ok": True, "closing": True}
    if cmd == "doctor":
        return {"ok": True, "python": sys.version.split()[0],
                "executable": sys.executable, "platform": sys.platform,
                "display": os.environ.get("DISPLAY", ""),
                "profile_dir": PROFILE_DIR, "headless": HEADLESS,
                "channel": CHANNEL or "chromium", "level": h.level_name,
                "locale": LOCALE, "timezone": TIMEZONE, "proxy": bool(PROXY)}

    # ---- navigation ------------------------------------------------------
    if cmd == "goto":
        r = page.goto(a("url", "about:blank"), wait_until=a("wait", "domcontentloaded"),
                      timeout=int(a("timeout", "45000")), referer=a("referer"))
        FRAME[0] = None
        _remember_page_wd(page)
        if a("dwell", "0") == "1":
            h.idle(float(a("idle", "1.5")))
        return {"ok": True, "url": page.url, "status": r.status if r else None,
                "title": _safe_title(page)}
    if cmd in ("back", "forward", "reload"):
        r = getattr(page, cmd if cmd != "reload" else "reload")(
            timeout=int(a("timeout", "30000")))
        FRAME[0] = None
        return {"ok": True, "url": page.url, "status": r.status if r else None}
    if cmd == "url":
        return {"ok": True, "url": page.url}
    if cmd == "title":
        return {"ok": True, "title": _safe_title(page), "url": page.url}

    # ---- interaction -----------------------------------------------------
    if cmd == "click":
        sel, nth = a("selector"), int(a("nth", "0"))
        if raw or FRAME[0] is not None:
            # Inside an iframe there are no viewport coordinates to curve
            # through, so this stays an element-level click — still a real
            # trusted event, just without the human approach.
            target = cur()
            els = target.query_selector_all(sel)
            if not els:
                raise LookupError(f"selector matched nothing: {sel!r}")
            idx = nth if nth >= 0 else len(els) + nth
            els[idx].click(timeout=int(a("timeout", "15000")),
                           click_count=int(a("clicks", "1")),
                           button=a("button", "left"))
        else:
            h.click(sel, nth=nth, button=a("button", "left"),
                    clicks=int(a("clicks", "1")))
        return {"ok": True, "url": page.url}
    if cmd == "clickxy":
        h.speed = 0.0 if raw else h.speed
        h.click_at(float(a("x", "0")), float(a("y", "0")),
                   button=a("button", "left"), clicks=int(a("clicks", "1")))
        return {"ok": True, "url": page.url}
    if cmd == "hover":
        if FRAME[0] is not None:
            cur().hover(a("selector"), timeout=int(a("timeout", "15000")))
        else:
            h.hover(a("selector"), nth=int(a("nth", "0")))
        return {"ok": True}
    if cmd == "drag":
        if FRAME[0] is not None:
            cur().locator(a("selector")).drag_to(cur().locator(a("to")))
        else:
            h.drag(a("selector"), a("to"))
        return {"ok": True}
    if cmd in ("check", "uncheck"):
        want = cmd == "check"
        nth = int(a("nth", "0"))
        # Human._el resolves against the top document, so inside a frame the
        # humanised branch would search the wrong document and report a missing
        # selector for an element that is right there in the frame.
        if raw or FRAME[0] is not None:
            (cur().check if want else cur().uncheck)(a("selector"))
        else:
            el = h._el(a("selector"), nth)
            if bool(el.is_checked()) != want:
                h.click(a("selector"), nth=nth)
        return {"ok": True}
    if cmd == "focus":
        cur().focus(a("selector"))
        return {"ok": True}
    if cmd == "select":
        sel = a("selector")
        if a("label") is not None:
            cur().select_option(sel, label=a("label"))
        elif a("index") is not None:
            cur().select_option(sel, index=int(a("index")))
        else:
            cur().select_option(sel, value=a("value"))
        return {"ok": True, "selected": cur().eval_on_selector(sel, "e => e.value")}
    if cmd == "type":
        sel = a("selector")
        if raw:
            if sel:
                cur().fill(sel, "")
            page.keyboard.type(a("text", ""))
        elif sel and FRAME[0] is not None:
            # Focus the field inside the frame with an element click; the
            # keyboard is page-level and still delivers to the focused element,
            # so typing keeps its human cadence either way. Clearing must also
            # be keyboard-only — Human._clear_field triple-clicks at the tracked
            # cursor, which lives in the top document and would steal focus.
            cur().click(sel, timeout=int(a("timeout", "15000")))
            if a("clear", "1") == "1":
                page.keyboard.press(f"{SELECT_ALL_MOD}+A")
                page.keyboard.press("Delete")
            h.type_text(a("text", ""), clear=False,
                        submit=a("submit", "0") == "1",
                        typo_rate=float(a("typo", "0.02")))
        elif sel:
            h.type_into(sel, a("text", ""), nth=int(a("nth", "0")),
                        clear=a("clear", "1") == "1",
                        submit=a("submit", "0") == "1",
                        typo_rate=float(a("typo", "0.02")))
        else:
            h.type_text(a("text", ""), clear=a("clear", "0") == "1",
                        submit=a("submit", "0") == "1",
                        typo_rate=float(a("typo", "0.02")))
        return {"ok": True}
    if cmd == "fill":  # instant set-value: form plumbing, not a user action
        cur().fill(a("selector"), a("text", ""), timeout=int(a("timeout", "15000")))
        return {"ok": True}
    if cmd == "press":
        h.press(a("key", "Enter"), repeat=int(a("repeat", "1")))
        return {"ok": True}
    if cmd == "scroll":
        if a("to"):
            h.scroll_to(a("to"))
        else:
            h.scroll(int(a("px", "800")), up=a("up", "0") == "1")
        return {"ok": True, "scrollY": page.evaluate("() => window.scrollY")}
    if cmd == "idle":
        h.idle(float(a("seconds", "1")))
        return {"ok": True}
    if cmd == "wait":
        page.wait_for_timeout(int(a("ms", "1000")))
        return {"ok": True}
    if cmd == "waitfor":
        cur().wait_for_selector(a("selector"), state=a("state", "visible"),
                                timeout=int(a("timeout", "20000")))
        return {"ok": True}
    if cmd == "waiturl":
        page.wait_for_url(a("pattern", "**"), timeout=int(a("timeout", "30000")))
        return {"ok": True, "url": page.url}
    if cmd in ("waitload", "waitidle"):
        page.wait_for_load_state("networkidle" if cmd == "waitidle" else "load",
                                 timeout=int(a("timeout", "45000")))
        return {"ok": True}

    # ---- reading ---------------------------------------------------------
    if cmd == "text":
        return {"ok": True, "text": _clip(cur().inner_text(a("selector", "body"),
                                                           timeout=int(a("timeout", "15000"))),
                                          a("max", "12000"))}
    if cmd == "html":
        sel = a("selector")
        html = cur().inner_html(sel) if sel else page.content()
        return {"ok": True, "html": html[:int(a("max", "200000"))]}
    if cmd == "attr":
        return {"ok": True, "value": cur().get_attribute(a("selector"), a("name", "href"))}
    if cmd == "value":
        return {"ok": True, "value": cur().input_value(a("selector"))}
    if cmd == "count":
        return {"ok": True, "count": len(cur().query_selector_all(a("selector")))}
    if cmd == "exists":
        return {"ok": True, "exists": cur().query_selector(a("selector")) is not None}
    if cmd == "links":
        els = cur().query_selector_all("a[href]")
        return {"ok": True, "links": [{"text": _clip(e.inner_text(), 120),
                                       "href": e.get_attribute("href")}
                                      for e in els[:int(a("n", "200"))]]}
    if cmd == "eval":
        # Patchright runs evaluate() in an isolated world by default, so page
        # globals (window.fpPromise, grecaptcha, a framework's store) are
        # invisible. `--main` opts into the page's own world when you need them.
        js, target = a("js", "null"), cur()
        if a("main", "0") == "1":
            try:
                return {"ok": True, "result": target.evaluate(js, isolated_context=False)}
            except TypeError as e:
                # Only swallow the "patchright is too old for isolated_context"
                # signature error — a TypeError thrown by the page's own
                # JavaScript must not be silently re-run in another world.
                if "isolated_context" not in str(e):
                    raise
                return {"ok": True, "result": target.evaluate(js),
                        "warning": "isolated_context unsupported; ran isolated"}
        return {"ok": True, "result": target.evaluate(js)}

    # ---- artifacts -------------------------------------------------------
    if cmd == "screenshot":
        wd = _wd()
        _shot_n[wd] = _shot_n.get(wd, 0) + 1
        path = _abs(a("path"), wd) if a("path") else os.path.join(_art(wd), f"shot-{_shot_n[wd]}.png")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        sel = a("selector")
        if sel:
            cur().locator(sel).screenshot(path=path)
        else:
            page.screenshot(path=path, full_page=a("full", "0") == "1")
        return {"ok": True, "path": os.path.abspath(path)}
    if cmd == "pdf":
        wd = _wd()
        path = _abs(a("path"), wd) if a("path") else os.path.join(_art(wd), "page.pdf")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        page.pdf(path=path, print_background=True,
                 format=a("format", "A4"))
        return {"ok": True, "path": os.path.abspath(path)}
    if cmd == "setfiles":
        sel = a("selector", "input[type=file]")
        paths = [_abs(p) for p in a("path", "").split(os.pathsep) if p]
        if not paths:
            # set_input_files([]) *clears* the field, so an omitted path used to
            # look like success while destroying the caller's selection.
            raise ValueError("setfiles needs a path (separate several with "
                             f"{os.pathsep!r})")
        if a("chooser", "0") == "1":
            with page.expect_file_chooser(timeout=int(a("timeout", "20000"))) as fc:
                (h.click(sel) if not raw else cur().click(sel))
            fc.value.set_files(paths[0] if len(paths) == 1 else paths)
        else:
            cur().set_input_files(sel, paths[0] if len(paths) == 1 else paths,
                                  timeout=int(a("timeout", "20000")))
        return {"ok": True}
    if cmd == "download":
        with page.expect_download(timeout=int(a("timeout", "60000"))) as dl:
            (h.click(a("selector")) if not raw else cur().click(a("selector")))
        d = dl.value
        save = _abs(a("save")) if a("save") else os.path.join(_art(_wd()), d.suggested_filename)
        os.makedirs(os.path.dirname(os.path.abspath(save)), exist_ok=True)
        d.save_as(save)
        return {"ok": True, "path": os.path.abspath(save), "url": d.url}
    if cmd == "dialog":
        DIALOG_MODE = a("mode", "accept")
        return {"ok": True, "mode": DIALOG_MODE, "last": dict(_last_dialog)}

    # ---- session / browser state ----------------------------------------
    if cmd == "cookies":
        return {"ok": True, "cookies": ctx.cookies(a("url"))}
    if cmd == "addcookies":
        with open(_abs(a("path")), encoding="utf-8") as fh:
            cookies = _cookie_list(json.load(fh))
        ctx.add_cookies(_normalise_cookies(cookies))
        return {"ok": True, "count": len(cookies)}
    if cmd == "setcookies":
        cookies = _cookie_list(json.loads(a("json", "[]")))
        ctx.add_cookies(_normalise_cookies(cookies))
        return {"ok": True, "count": len(cookies)}
    if cmd == "clearcookies":
        ctx.clear_cookies()
        return {"ok": True}
    if cmd == "storage":
        return {"ok": True, "local": page.evaluate(
            "() => Object.fromEntries(Object.entries(localStorage))"),
            "session": page.evaluate(
                "() => Object.fromEntries(Object.entries(sessionStorage))")}
    if cmd == "ua":
        return {"ok": True, "ua": page.evaluate("() => navigator.userAgent"),
                "platform": page.evaluate("() => navigator.platform"),
                "webdriver": page.evaluate("() => navigator.webdriver"),
                "plugins": page.evaluate("() => navigator.plugins.length"),
                "languages": page.evaluate("() => navigator.languages"),
                "tz": page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone"),
                "cores": page.evaluate("() => navigator.hardwareConcurrency"),
                "mem": page.evaluate("() => navigator.deviceMemory"),
                "screen": page.evaluate("() => [screen.width, screen.height, screen.availWidth, devicePixelRatio]")}
    if cmd == "tabs":
        return {"ok": True, "tabs": [{"i": i, "url": p.url, "title": _safe_title(p)}
                                     for i, p in enumerate(ctx.pages)]}
    if cmd == "newtab":
        p = ctx.new_page()
        _attach_page(p)
        if a("url"):
            p.goto(a("url"), wait_until=a("wait", "domcontentloaded"),
                   timeout=int(a("timeout", "45000")))
        if a("focus", "1") == "1":
            p.bring_to_front()
        PAGE[0] = ctx.pages.index(p)
        FRAME[0] = None
        _remember_page_wd(p)
        return {"ok": True, "tabs": len(ctx.pages), "tab": PAGE[0], "url": p.url}
    if cmd == "tab":
        i = int(a("i", "-1"))
        p = ctx.pages[i]
        p.bring_to_front()
        # Pin the selection: everything after this acts on this tab, not on
        # whichever one happens to be newest.
        PAGE[0] = i if i >= 0 else len(ctx.pages) + i
        FRAME[0] = None
        return {"ok": True, "tab": PAGE[0], "url": p.url, "title": _safe_title(p)}
    if cmd == "closetab":
        i = int(a("i", "-1"))
        ctx.pages[i].close()
        PAGE[0] = None
        FRAME[0] = None
        return {"ok": True, "tabs": len(ctx.pages)}
    if cmd == "frame":
        sel, f = a("selector"), None
        if sel:
            el = page.query_selector(sel)
            f = el.content_frame() if el is not None else None
            if el is not None and f is None:  # iframe still attaching
                page.wait_for_timeout(500)
                f = el.content_frame()
            if f is None:
                return {"ok": False, "error": f"no content frame for {sel!r}"}
        else:
            pat = a("url")
            f = next((fr for fr in page.frames if pat and pat in fr.url), None)
            if f is None:
                return {"ok": False, "error": f"no frame matching {pat!r}",
                        "frames": [fr.url for fr in page.frames]}
        FRAME[0] = f
        return {"ok": True, "frame": f.url}
    if cmd == "mainframe":
        FRAME[0] = None
        return {"ok": True}
    if cmd == "frames":
        return {"ok": True, "frames": [fr.url for fr in page.frames]}
    if cmd == "block":
        ctx.route(re.compile(a("pattern")), lambda route: route.abort())
        return {"ok": True}
    if cmd == "unblock":
        ctx.unroute_all(behavior="ignoreErrors")
        return {"ok": True}

    # ---- captured traffic ------------------------------------------------
    if cmd == "requests":
        n = int(a("n", "50"))
        only, method = a("filter"), (a("method") or "").upper()
        items = [e for e in recent
                 if (not only or only in e.get("url", ""))
                 and (not method or e.get("method") == method)]
        return {"ok": True, "count": len(items), "method": method,
                "log": os.path.join(_art(_wd()), "requests.jsonl"),
                "items": items[-n:]}
    if cmd == "ws":
        n = int(a("n", "50"))
        only = a("filter")
        items = [e for e in ws_recent if not only or only in e.get("url", "")]
        return {"ok": True, "count": len(items), "items": items[-n:]}
    if cmd == "clear":
        recent.clear()
        ws_recent.clear()
        try:
            open(os.path.join(_art(_wd()), "requests.jsonl"), "w").close()
        except Exception:
            pass
        return {"ok": True}

    raise ValueError(f"unknown command: {cmd}")


def _safe_title(page):
    try:
        return page.title()
    except Exception:
        return ""


def _clip(s, n):
    n = int(n)
    return s if len(s) <= n else s[:n] + f"\n...[truncated {len(s) - n} chars]"


def _cookie_list(data):
    """Unwrap a cookie payload: a bare list, or an object with a ``cookies`` key.

    The wrapper form is what this tool's own ``cookies`` command emits, so
    ``bu cookies > jar.json`` round-trips through ``bu addcookies jar.json``.
    """
    if isinstance(data, dict):
        data = data.get("cookies")
    if not isinstance(data, list):
        raise ValueError("expected a list of cookies, or an object with a "
                         "'cookies' list")
    return data


def _normalise_cookies(cookies):
    """Accept Chrome-extension / EditThisCookie exports as well as Playwright's."""
    out = []
    for c in cookies:
        c = dict(c)
        ss = c.get("sameSite")
        if ss in (None, "unspecified", "no_restriction", "None"):
            c["sameSite"] = "None" if ss == "no_restriction" else "Lax"
        elif str(ss).lower() == "lax":
            c["sameSite"] = "Lax"
        elif str(ss).lower() == "strict":
            c["sameSite"] = "Strict"
        else:
            c["sameSite"] = "None"
        for junk in ("hostOnly", "storeId", "id", "session"):
            c.pop(junk, None)
        if "expirationDate" in c:
            c["expires"] = int(c.pop("expirationDate"))
        if c.get("expires") in (-1, 0):
            c.pop("expires", None)
        out.append(c)
    return out


# --------------------------------------------------------------------------
# HTTP front end
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the daemon log readable
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, why="unauthorized"):
        self._json(401, {"ok": False, "error": why})

    def _trusted_caller(self):
        """Reject the request shapes a web page can be tricked into sending.

        Binding to loopback is not enough on its own. Any page the user has
        open can fire a no-cors GET at 127.0.0.1:PORT and the request still
        executes here — the page cannot read the reply, but this daemon's
        side effects (navigate, /eval, screenshot to a path) have already
        happened. Chrome labels every request with Sec-Fetch-Site, and a
        loopback client (curl, the CLI) sends none, so: refuse anything the
        browser marks as coming from another site, and refuse anything that
        carries an Origin at all. Host is checked too, which is what DNS
        rebinding would have to defeat.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("none", "same-origin"):
            return False
        # Only a browser sets Origin; the CLI is a plain HTTP client and never
        # does. There is no page of ours to be the same origin as, so any
        # Origin at all means a page somewhere is driving this.
        if self.headers.get("Origin"):
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        cmd = u.path.strip("/")

        if not self._trusted_caller():
            return self._deny("bad origin")
        if not hmac.compare_digest(q.get("token", [""])[0], TOKEN):
            return self._deny()

        # Liveness must answer even while a slow command is running: the CLI
        # reads a ping timeout as "no daemon", starts a second one, and that
        # second daemon clobbers the state file of the one that was fine —
        # after which every command fails and `bu stop` reports success while
        # the browser keeps running.
        if cmd == "ping" and not _Q.empty():
            return self._json(200, _static_ping(busy=True))

        box = {"wd": _safe_wd(q.get("wd", [None])[0]) or os.getcwd(),
               "done": threading.Event()}
        _Q.put((cmd, q, box))
        # A generous ceiling, deliberately not tied to the command's own
        # `--timeout`: that one bounds a Playwright wait inside the command,
        # and using it here would cut off a long human-paced type.
        if not box["done"].wait(timeout=600.0):
            return self._json(504, {"ok": False, "error": "command timed out"})
        self._json(box.get("code", 500), box.get("out", {"ok": False}))

        if cmd == "close":
            _Q.put(None)   # worker drains, closes the browser, exits


def _static_ping(busy=False):
    """What a ping can answer without touching the browser."""
    return {"ok": True, "busy": busy, "url": "", "title": "",
            "headless": HEADLESS, "channel": CHANNEL or "chromium",
            "profile": PROFILE, "level": LEVEL, "tab": None, "tabs": None}


#: Commands waiting for the browser owner. Exactly one worker thread consumes
#: it, because Playwright's sync API is bound to the thread that created it.
_Q: queue.Queue = queue.Queue()


def _worker():
    """The only thread that touches Playwright, for the daemon's whole life."""
    try:
        boot()
    except Exception as e:
        # Nothing can be served without a browser, and a half-started daemon
        # would answer the port forever while every command times out. Fail
        # loudly so `bu start` reports it instead of hanging.
        print(f"[bu] boot failed: {type(e).__name__}: {e}", file=sys.stderr,
              flush=True)
        os._exit(1)
    while True:
        item = _Q.get()
        if item is None:
            break
        cmd, q, box = item
        _CURRENT_WD[0] = box["wd"]
        try:
            box["out"], box["code"] = handle(cmd, q), 200
        except Exception as e:
            box["out"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            box["code"] = 400
        box["done"].set()

    # Drain, then shut down. os._exit() alone orphans Chrome, which leaves a
    # stale SingletonLock and a dead ProcessSingleton socket in the profile —
    # the next launch on that profile then fails. Closing the context lets
    # Chrome release its own locks.
    time.sleep(0.15)   # let the last HTTP response flush
    try:
        ctx.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
    os._exit(0)


def main():
    threading.Thread(target=_worker, daemon=False).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"[bu] ready on 127.0.0.1:{PORT} profile={PROFILE} "
          f"headless={HEADLESS} channel={CHANNEL or 'chromium'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
