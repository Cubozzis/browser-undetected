#!/usr/bin/env python3
"""Live end-to-end check: boot a browser, drive a real form, assert it worked.

    python3 tests/smoke.py

Boots its own daemon on a throwaway port and profile, serves a fixture page on
localhost, and exercises the paths that break silently — navigation, human
typing landing in the field, a click submitting the form, scrolling, artifacts,
network capture, form controls, cookies and frames.

It uses its own BU_HOME, so it cannot touch — least of all `stop` — the daemon
a real session is using. Takes about a minute on a cold start, because the
first run creates a virtualenv and may download a browser.

Exits 0 on success, 1 with a readable diff on failure.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BU = os.path.join(HERE, "..", "scripts", "bu.py")
PORT = os.environ.get("BU_SMOKE_PORT", "9333")
PROFILE = "selftest"
# A smoke run must never touch — least of all `stop` — the daemon a real session
# is using. Its own BU_HOME separates venv, profiles, state and logs entirely.
# The default path is stable so the one-off venv + browser download is paid once
# instead of on every run; set BU_SMOKE_HOME to move or discard it.
HOME = (os.environ.get("BU_SMOKE_HOME")
        or os.path.join(os.path.expanduser("~"), ".browser-undetected-smoke"))

FIXTURE = """<!doctype html><html><head><title>BU Smoke</title>
<script>window.__pageGlobal = "from-page"; window.__clicks = 0;
document.addEventListener("click", function (e) {
  window.__clicks++;
  window.__last = [e.clientX, e.clientY];
}, true);</script></head><body>
<h1 id="h">ready</h1>
<ul id="list"><li class="item">one</li><li class="item">two</li></ul>
<form id="f" onsubmit="event.preventDefault();
  document.getElementById('h').textContent = 'submitted:' + document.getElementById('q').value">
  <input id="q" name="q" type="text" placeholder="type here">
  <button id="go" type="submit">Go</button>
</form>
<div id="box" style="width:240px;height:90px;background:#eef">box</div>
<select id="sel"><option value="a">A</option><option value="b">B</option></select>
<input id="cb" type="checkbox">
<span class="dup" onclick="window.__which='first'">x</span>
<span class="dup" onclick="window.__which='second'">y</span>
<iframe id="fr" src="frame.html" style="width:420px;height:180px"></iframe>
<div style="height:2500px"></div>
<p id="bottom">bottom</p>
</body></html>
"""

FRAME_FIXTURE = """<!doctype html><html><head><title>Inner</title></head><body>
<input id="fq" type="text" placeholder="inner field">
<input id="fcb" type="checkbox">
<button id="fb" onclick="document.getElementById('out').textContent='clicked'">press</button>
<div id="out"></div>
</body></html>
"""

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: {detail}")
        FAILED.append(name)


WORKDIR = os.getcwd()


def bu(*args, expect_ok=True):
    """Run the CLI *from the workdir*, which is where artifacts must land."""
    env = dict(os.environ, BU_PORT=PORT, BU_HOME=HOME)
    r = subprocess.run([sys.executable, BU, *args], capture_output=True,
                       text=True, env=env, cwd=WORKDIR, timeout=180)
    try:
        out = json.loads(r.stdout)
    except Exception:
        raise AssertionError(
            f"`bu {' '.join(args)}` did not print JSON.\n"
            f"stdout: {r.stdout[:800]}\nstderr: {r.stderr[:800]}")
    out["_rc"] = r.returncode
    if expect_ok:
        assert out.get("ok"), f"`bu {' '.join(args)}` failed: {out}"
    return out


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory):
    handler = functools.partial(Quiet, directory=directory)
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main():
    global WORKDIR
    tmp = tempfile.mkdtemp(prefix="bu-smoke-")
    WORKDIR = tmp
    os.chdir(tmp)
    srv, port = serve(tmp)
    url = f"http://127.0.0.1:{port}/index.html"
    with open(os.path.join(tmp, "index.html"), "w") as f:
        f.write(FIXTURE)
    with open(os.path.join(tmp, "frame.html"), "w") as f:
        f.write(FRAME_FIXTURE)
    print(f"fixture: {url}\nworkdir: {tmp}\n")

    try:
        print("startup")
        # --force so a daemon left behind by a crashed earlier run is replaced
        # rather than silently reused with stale settings.
        st = bu("start", "--port", PORT, "--profile", PROFILE, "--level", "fast",
                "--force")
        check("daemon started", st.get("ok"), st)
        check("uses a real browser channel",
              st.get("channel") in ("chrome", "msedge", "chromium"), st.get("channel"))

        print("\nnavigation")
        r = bu("goto", url, "--dwell")
        check("goto returns 200", r.get("status") == 200, r.get("status"))
        check("title read", r.get("title") == "BU Smoke", r.get("title"))

        print("\nfingerprint")
        r = bu("ua")
        check("navigator.webdriver is false", r.get("webdriver") is False, r.get("webdriver"))
        check("plugins present", (r.get("plugins") or 0) > 0, r.get("plugins"))
        check("real screen size", (r.get("screen") or [0])[0] >= 1024, r.get("screen"))
        check("user agent is a browser", "Mozilla" in (r.get("ua") or ""), r.get("ua"))
        # A GPU-less box reports no WebGL at all unless the daemon passes
        # --enable-unsafe-swiftshader; that absence is itself a bot signal, so
        # assert on the context, not on `typeof`.
        r = bu("eval", "!!document.createElement('canvas').getContext('webgl')")
        check("WebGL context is available", r.get("result") is True, r.get("result"))

        print("\nhuman typing")
        t0 = time.time()
        # deliberately split across argv words: the last positional must
        # swallow the rest instead of dropping everything after the first
        bu("type", "#q", "hello", "world", "--submit")
        typed_s = time.time() - t0
        r = bu("text", "#h")
        check("multi-word text reached the form",
              r.get("text") == "submitted:hello world", r.get("text"))
        check("typing was human-paced (not instant)", typed_s > 1.0, f"{typed_s:.2f}s")
        check("but not absurdly slow", typed_s < 45, f"{typed_s:.2f}s")

        print("\nclicking")
        # The fixture counts every click in the page's own world, so a click
        # that never reached the document cannot pass this. Earlier commands
        # (focusing the field, `--dwell`) have already clicked, so compare the
        # delta, not the absolute count.
        before = bu("eval", "window.__clicks", "--main").get("result") or 0
        bu("click", "#box")
        r = bu("eval", "window.__clicks", "--main")
        check("click on a selector reached the page", r.get("result") == before + 1,
              f"{before} -> {r.get('result')}")
        r = bu("eval", "--main", """(() => {
          const b = document.getElementById('box').getBoundingClientRect();
          const [x, y] = window.__last;
          return x >= b.left && x <= b.right && y >= b.top && y <= b.bottom;
        })()""")
        check("click landed inside the element's box", r.get("result") is True, r.get("result"))

        bu("clickxy", "120", "200")
        r = bu("eval", "window.__last", "--main")
        check("clickxy lands on the given coordinates",
              isinstance(r.get("result"), list)
              and abs(r["result"][0] - 120) <= 2 and abs(r["result"][1] - 200) <= 2,
              r.get("result"))

        bu("click", ".dup", "--nth", "1")
        r = bu("eval", "window.__which", "--main")
        check("click --nth picks the second match", r.get("result") == "second", r.get("result"))

        print("\nform controls")
        r = bu("select", "#sel", "--value", "b")
        check("select --value reports the new value", r.get("selected") == "b", r.get("selected"))
        r = bu("value", "#sel")
        check("select actually changed the element", r.get("value") == "b", r.get("value"))

        bu("check", "#cb")
        r = bu("eval", "document.getElementById('cb').checked")
        check("check ticks the box", r.get("result") is True, r.get("result"))
        bu("uncheck", "#cb")
        r = bu("eval", "document.getElementById('cb').checked")
        check("uncheck clears it", r.get("result") is False, r.get("result"))

        bu("fill", "#q", "filled")
        r = bu("value", "#q")
        check("fill sets the value", r.get("value") == "filled", r.get("value"))
        bu("type", "#q", "typed", "--clear")
        r = bu("value", "#q")
        check("type --clear replaces rather than appends", r.get("value") == "typed", r.get("value"))
        bu("fill", "#q", "")             # leave the field clean for the form test

        print("\nreading")
        r = bu("count", ".item")
        check("count finds both items", r.get("count") == 2, r.get("count"))
        r = bu("exists", "#bottom")
        check("exists true", r.get("exists") is True)
        r = bu("exists", "#not-a-real-element")
        check("exists false for missing", r.get("exists") is False)
        r = bu("attr", "#q", "placeholder")
        check("attr read", r.get("value") == "type here", r.get("value"))
        r = bu("eval", "1 + 1")
        check("eval", r.get("result") == 2, r.get("result"))
        # A page global written by the fixture's own <script> is the only way to
        # tell the two worlds apart: writing and reading window.__probe inside
        # one eval works in either world and would pass while --main was broken.
        r = bu("eval", "typeof window.__pageGlobal")
        check("isolated eval cannot see page globals",
              r.get("result") == "undefined", r.get("result"))
        r = bu("eval", "window.__pageGlobal", "--main")
        check("eval --main reaches the page's own world",
              r.get("result") == "from-page", r.get("result"))
        r = bu("eval", "--main", "window.__pageGlobal")
        check("--main also works before its expression",
              r.get("result") == "from-page", r.get("result"))

        print("\nwaiting")
        t0 = time.time()
        check("wait <ms> positional accepted", bu("wait", "700").get("ok") is True)
        waited = time.time() - t0
        check("wait actually waited", waited >= 0.6, f"returned after {waited:.2f}s")
        check("idle <sec> positional accepted", bu("idle", "1").get("ok") is True)

        print("\nscrolling")
        # 1200, not the 800 default — a dropped positional would still "pass"
        r = bu("scroll", "1200")
        check("scroll <px> positional is honoured", (r.get("scrollY") or 0) > 900,
              r.get("scrollY"))
        bu("eval", "window.scrollTo(0, 0)")   # else "reached the bottom" is free
        r = bu("eval", "window.scrollY")
        check("page is back at the top before the --to test", r.get("result") == 0, r.get("result"))
        bu("scroll", "800", "--to", "#bottom")
        r = bu("eval", "window.scrollY")
        check("scroll --to reached the bottom", (r.get("result") or 0) > 1000, r.get("result"))

        print("\nartifacts")
        r = bu("screenshot")
        path = r.get("path", "")
        check("screenshot written", os.path.exists(path) and os.path.getsize(path) > 5000,
              f"{path} size={os.path.getsize(path) if os.path.exists(path) else 'missing'}")
        check("screenshot lands in ./.browser",
              os.path.basename(os.path.dirname(path)) == ".browser", path)
        viewport_bytes = os.path.getsize(path)
        r = bu("screenshot", "--full")
        full = r.get("path", "")
        check("full-page screenshot written", os.path.exists(full), full)
        # The fixture page is ~2500px taller than the window, so a --full shot
        # that silently behaved like a viewport shot cannot be this much bigger.
        check("full-page screenshot is really full-page",
              os.path.getsize(full) > viewport_bytes * 1.5,
              f"full={os.path.getsize(full) if os.path.exists(full) else 'missing'} "
              f"viewport={viewport_bytes}")
        r = bu("pdf")
        pdf = r.get("path", "")
        with open(pdf, "rb") as fh:
            magic = fh.read(5)
        check("pdf is a real PDF", magic == b"%PDF-" and os.path.getsize(pdf) > 1000,
              f"{pdf} magic={magic!r}")

        print("\ntraffic capture")
        r = bu("requests", "--filter", "index.html")
        check("the document request was captured",
              any("index.html" in i.get("url", "") for i in r.get("items", [])),
              r.get("count"))
        r = bu("requests", "--filter", "no-such-path-anywhere")
        check("a non-matching --filter returns nothing", r.get("count") == 0, r.get("count"))
        r = bu("requests", "--filter", "index.html", "--method", "POST")
        check("--method filters GET requests out", r.get("count") == 0, r.get("count"))
        log = os.path.join(tmp, ".browser", "requests.jsonl")
        check("requests.jsonl written", os.path.exists(log) and os.path.getsize(log) > 0, log)

        print("\nframes")
        bu("scroll", "--to", "#fr")
        r = bu("frame", "#fr")
        check("frame selected", r.get("ok") is True, r)
        r = bu("count", "#fb")           # selector exists only inside the iframe
        check("selectors resolve inside the frame", r.get("count") == 1, r)
        bu("click", "#fb")
        r = bu("text", "#out")
        check("click landed inside the frame", r.get("text") == "clicked", r.get("text"))
        bu("type", "#fq", "inner text")
        r = bu("value", "#fq")
        check("typing landed inside the frame", r.get("value") == "inner text", r.get("value"))
        # Human._el resolves against the top document, so this path needs its
        # own element-level branch or it looks for #fcb on the wrong page.
        bu("check", "#fcb")
        r = bu("eval", "document.getElementById('fcb').checked")
        check("check works inside the frame", r.get("result") is True, r.get("result"))
        check("mainframe resets", bu("mainframe").get("ok") is True)
        r = bu("exists", "#fb")
        check("selectors go back to the top document", r.get("exists") is False, r)

        print("\ntabs")
        r = bu("cookies")
        check("cookies readable", isinstance(r.get("cookies"), list))

        print("\ncookie round-trip")
        jar = json.dumps([{"name": "smoke", "value": "1", "domain": "127.0.0.1",
                           "path": "/"}])
        r = bu("setcookies", jar)
        check("setcookies reports one cookie", r.get("count") == 1, r)
        r = bu("cookies")
        check("the cookie comes back",
              any(c.get("name") == "smoke" and c.get("value") == "1"
                  for c in r.get("cookies", [])), r.get("cookies"))
        # `bu cookies` output has to feed straight back into `addcookies`, or
        # the save-and-restore flow the docs advertise does not actually work.
        jar_file = os.path.join(tmp, "jar.json")
        with open(jar_file, "w") as fh:
            json.dump(r, fh)
        r = bu("eval", "document.cookie", "--main")
        check("the cookie is visible to the page", "smoke=1" in (r.get("result") or ""),
              r.get("result"))
        check("clearcookies succeeds", bu("clearcookies").get("ok") is True)
        r = bu("cookies")
        check("clearcookies removed it",
              not any(c.get("name") == "smoke" for c in r.get("cookies", [])),
              r.get("cookies"))
        r = bu("addcookies", jar_file)
        check("addcookies reloads a saved jar", r.get("count") == 1, r)
        r = bu("cookies")
        check("the restored cookie is back",
              any(c.get("name") == "smoke" for c in r.get("cookies", [])), r.get("cookies"))

        bu("goto", url)                  # make sure tab 0 is the main page
        bu("newtab", url.replace("index.html", "frame.html"))
        r = bu("tabs")
        check("newtab opened", len(r.get("tabs", [])) == 2, r.get("tabs"))
        r = bu("tab", "0")
        check("tab 0 selected", r.get("tab") == 0, r.get("tab"))
        r = bu("title")
        check("commands now target tab 0", r.get("title") == "BU Smoke", r.get("title"))
        r = bu("tab", "1")
        check("tab 1 selected", r.get("tab") == 1, r.get("tab"))
        r = bu("title")
        check("commands now target tab 1", r.get("title") == "Inner", r.get("title"))
        bu("closetab", "1")
        r = bu("tabs")
        check("closetab closed it", len(r.get("tabs", [])) == 1, r.get("tabs"))
        r = bu("reload")
        check("reload", r.get("ok") is True)

        print("\nflag plumbing (restart with --capture-bodies)")
        r = bu("restart", "--capture-bodies", "--level", "fast")
        check("restart succeeds", r.get("ok") is True, r)
        bu("goto", url, "--wait", "networkidle")
        r = bu("clear")
        bu("goto", url, "--wait", "networkidle")
        r = bu("requests", "--filter", "index.html")
        check("--capture-bodies reached the daemon",
              any(i.get("body") for i in r.get("items", [])),
              f"no response body captured: {r.get('count')} items")

        print("\nerror handling")
        r = bu("click", "#definitely-not-here", expect_ok=False)
        check("bad selector returns ok=false", r.get("ok") is False, r)
        check("bad selector exits non-zero", r.get("_rc") == 1, r.get("_rc"))
        r = bu("click", "#definitely-not-here", expect_ok=False)
        check("error names the selector", "#definitely-not-here" in json.dumps(r), r)

        note = st.get("channel")
        if note == "chromium":
            print("\nnote: ran on bundled Chromium (no system Chrome). Install "
                  "Google Chrome for the strongest fingerprint.")
    finally:
        subprocess.run([sys.executable, BU, "stop", "--port", PORT],
                       capture_output=True,
                       env=dict(os.environ, BU_PORT=PORT, BU_HOME=HOME))
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
