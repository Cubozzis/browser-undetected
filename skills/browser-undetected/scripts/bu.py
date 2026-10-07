#!/usr/bin/env python3
"""bu — command-line front end for the browser-undetected skill.

Two jobs:

1. **Bootstrap.** Make sure a Python environment with Patchright and a real
   browser exists, on Windows, macOS or Linux, without the caller knowing or
   caring which. First run creates ``~/.browser-undetected/venv`` and downloads
   Chromium; later runs are instant.
2. **Drive.** Forward commands to the persistent daemon over 127.0.0.1 and
   print its JSON reply, so any agent that can run a shell command can pilot a
   browser across as many separate invocations as it likes.

Everything prints JSON on stdout. Errors are JSON too — agents can read them.

    bu goto https://example.com
    bu type "#search" "stealth browser" --submit
    bu screenshot
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""

HOME = os.environ.get("BU_HOME") or os.path.join(os.path.expanduser("~"), ".browser-undetected")
DEFAULT_PORT = os.environ.get("BU_PORT", "9317")


def venv_python(venv):
    return os.path.join(venv, "Scripts" if IS_WIN else "bin", "python" + EXE)


def vpy(*parts):
    return os.path.join(HOME, *parts)


# --------------------------------------------------------------------------
# command spec: positional names + which flags are boolean
# --------------------------------------------------------------------------

SPEC = {
    "goto": ["url"], "back": [], "forward": [], "reload": [], "url": [], "title": [],
    "click": ["selector"], "clickxy": ["x", "y"], "hover": ["selector"],
    "drag": ["selector", "to"], "check": ["selector"], "uncheck": ["selector"],
    "focus": ["selector"], "select": ["selector"], "type": ["selector", "text"],
    "fill": ["selector", "text"], "press": ["key"], "scroll": ["px"], "idle": ["seconds"],
    "wait": ["ms"], "waitfor": ["selector"], "waiturl": ["pattern"],
    "waitload": [], "waitidle": [], "text": ["selector"], "html": ["selector"],
    "attr": ["selector", "name"], "value": ["selector"], "count": ["selector"],
    "exists": ["selector"], "links": [], "eval": ["js"], "screenshot": ["path"],
    "pdf": ["path"], "setfiles": ["selector", "path"], "download": ["selector"],
    "dialog": [], "cookies": [], "addcookies": ["path"], "setcookies": ["json"],
    "clearcookies": [], "storage": [], "ua": [], "tabs": [], "newtab": ["url"],
    "tab": ["i"], "closetab": ["i"], "frame": ["selector"], "mainframe": [],
    "frames": [], "block": ["pattern"], "unblock": [], "requests": [], "ws": [],
    "clear": [], "ping": [], "doctor": [], "close": [],
}

#: Flags that never take a value. Membership here is what makes flag placement
#: free: `--main 1+1` would otherwise swallow the expression as main's value and
#: leave `js` at its default. Every pure boolean the daemon reads belongs here.
BOOLS = {"full", "raw", "up", "dwell", "submit", "headless", "force",
         "capture-bodies", "chooser", "system", "quiet", "main", "clear"}


def parse_args(argv):
    """Split ``--flags`` from positionals, supporting ``--k v`` and ``--k=v``.

    Keys are normalised to underscores here, once, so that both the relay path
    and the local `start` path see the same names — `--capture-bodies` has to
    reach the daemon as `capture_bodies`, and `start` reads it directly.
    """
    pos, flags = [], {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--"):
            body = tok[2:]
            if "=" in body:
                k, v = body.split("=", 1)
                flags[k.replace("-", "_")] = v
            elif body in BOOLS:
                flags[body.replace("-", "_")] = "1"
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                flags[body.replace("-", "_")] = argv[i + 1]
                i += 1
            else:
                flags[body.replace("-", "_")] = "1"
        else:
            pos.append(tok)
        i += 1
    return pos, flags


def to_params(cmd, pos, flags):
    spec = SPEC.get(cmd)
    if spec is None:
        raise SystemExit(json.dumps({"ok": False, "error": f"unknown command: {cmd}"}))
    p = {}
    for idx, name in enumerate(spec):
        if idx >= len(pos):
            break
        # The last positional swallows every remaining word, so
        # `bu type "#q" hey there` types "hey there" rather than dropping it.
        p[name] = " ".join(pos[idx:]) if idx == len(spec) - 1 else pos[idx]
    p.update(flags)   # already underscore-normalised by parse_args
    return p


# --------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------

def log(msg, quiet=False):
    if not quiet:
        print(f"[bu] {msg}", file=sys.stderr, flush=True)


def die(obj):
    """Report a fatal error as JSON on stdout, then exit 1.

    ``raise SystemExit(str)`` prints to *stderr*: an agent that captures stdout
    would see an empty document instead of the reason it failed.
    """
    print(json.dumps(obj, indent=1, ensure_ascii=False))
    sys.exit(1)


def have_module(py, module):
    try:
        return subprocess.run([py, "-c", f"import {module}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode == 0
    except Exception:
        return False


def system_chrome():
    """Path to a real branded Google Chrome, or None.

    Only Google Chrome counts. Playwright's ``channel="chrome"`` resolves to a
    Google Chrome install and nothing else — Edge is a separate channel and
    Chromium is the bundled download — so treating "some Chromium-family
    browser exists" as "the chrome channel is available" makes the daemon
    launch a channel that is not there, on a machine where the bundled fallback
    was never downloaded either. That is the default path on stock Windows,
    where Edge is preinstalled and Chrome usually is not.
    """
    if IS_WIN:
        cands = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 os.path.expanduser(
                     "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    else:
        cands = ["google-chrome", "google-chrome-stable"]
    for c in cands:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def ensure_browser(py, channel, force=False, quiet=False):
    """Install the requested browser binary once, and record which one stuck.

    Real Google Chrome is the officially recommended target ("We recommend
    using Google Chrome instead of Chromium") because it carries the branding,
    codecs and update behaviour a bundled Chromium lacks. It is a ~150 MB
    download, so we cache the outcome in ``venv/.channel`` and only pay it once.
    """
    if channel not in ("chrome", "chromium", "msedge"):
        die({"ok": False, "error": f"unknown channel: {channel!r}",
             "hint": "use --channel chrome (a system Google Chrome), "
                     "--channel chromium (downloaded), or --channel msedge"})

    resolved_file = vpy("venv", ".channel")
    if not force and os.path.exists(resolved_file):
        with open(resolved_file) as f:
            return f.read().strip() or channel

    if channel == "chrome":
        # The "chrome" channel always resolves to a browser the OS already has;
        # it is never downloadable, so check before promising it.
        found = system_chrome()
        if found:
            log(f"using system browser: {found}", quiet)
            resolved = "chrome"
        else:
            log("no system Chrome found — the chrome channel needs one, so "
                "falling back to bundled Chromium. Install Google Chrome and "
                "rerun `bu start --force --channel chrome` for the strongest "
                "fingerprint.", quiet)
            channel = "chromium"
    if channel != "chrome":
        log(f"downloading {channel} (one-time, ~150MB)", quiet)
        r = subprocess.run([py, "-m", "patchright", "install", channel])
        if r.returncode != 0:
            # Recording this as "chromium" would turn a network failure into a
            # launch failure later, reported as a 60s timeout with no cause.
            die({"ok": False, "error": f"downloading {channel} failed",
                 "hint": "check network access, then retry `bu start --force`"})
        resolved = channel

    try:
        with open(resolved_file, "w") as f:
            f.write(resolved + "\n")
    except OSError:
        pass
    return resolved


def _venv_works(py):
    """Can this interpreter actually run? Catches a dangling venv symlink."""
    try:
        return subprocess.run([py, "-c", "pass"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=30).returncode == 0
    except Exception:
        return False


def ensure_env(force=False, system=False, quiet=False, channel="chrome"):
    """Return a python that can `import patchright` with browsers installed."""
    marker = vpy("venv", ".ok")
    py = venv_python(vpy("venv"))

    if system:
        py = sys.executable
    elif not os.path.exists(py) or not _venv_works(py):
        # `exists` is not enough: a venv whose base interpreter was upgraded or
        # renamed (routine after a Homebrew python bump) keeps a symlink that
        # resolves to nothing, and the next pip call dies with a traceback.
        if os.path.exists(py):
            log("the virtualenv's interpreter is broken; rebuilding it", quiet)
            shutil.rmtree(vpy("venv"), ignore_errors=True)
        log(f"creating virtualenv at {vpy('venv')}", quiet)
        try:
            subprocess.run([sys.executable, "-m", "venv", vpy("venv")], check=True)
        except Exception as e:
            log(f"venv creation failed ({e}); falling back to the current "
                f"interpreter. On Debian/Ubuntu install python3-venv.", quiet)
            py = sys.executable
    if not os.path.exists(py):
        py = sys.executable

    if force or not have_module(py, "patchright"):
        log("installing patchright (one-time, ~30s)", quiet)
        subprocess.run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], check=False)
        r = subprocess.run([py, "-m", "pip", "install", "-q", "-U", "patchright"])
        if r.returncode != 0:
            die({"ok": False, "error": "pip install patchright failed",
                 "hint": "check network access, then retry `bu doctor --force`"})
        try:
            os.remove(marker)
        except OSError:
            pass

    ensure_browser(py, channel, force=force, quiet=quiet)
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "w").write("ok\n")
    except OSError:
        pass
    return py


def xvfb_prefix(headless):
    """On a headless Linux box, wrap in a virtual display instead of going headless.

    Only relevant on Linux: Windows and macOS always have a display server, and
    a headless browser is measurably easier to fingerprint than a headful one.
    """
    if headless or IS_WIN or sys.platform == "darwin":
        return []
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return []
    xvfb = shutil.which("xvfb-run")
    if not xvfb:
        die({"ok": False, "error": "no DISPLAY and xvfb-run is not installed",
             "hint": "apt-get install -y xvfb   (or pass --headless)",
             "fallback": "runtimes without X should start with: bu start --headless"})
    return [xvfb, "-a", "--server-args=-screen 0 1920x1080x24"]


# --------------------------------------------------------------------------
# daemon lifecycle
# --------------------------------------------------------------------------

def state_path(port):
    return vpy("state", f"{port}.json")


def read_state(port):
    try:
        with open(state_path(port)) as f:
            return json.load(f)
    except Exception:
        return {}


def http(port, cmd, params=None, token=None, timeout=180):
    params = dict(params or {})
    params.setdefault("wd", os.getcwd())
    if token:
        params["token"] = token
    url = f"http://127.0.0.1:{port}/{cmd}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "_down": True, "error": f"{type(e).__name__}: {e}"}


def alive(port):
    """Is a daemon of ours answering on this port?

    Returns "yes", "no" (nothing listening) or "other" (something *is*
    listening but would not accept our token, or stayed silent). The
    distinction matters: treating "other" as "no" makes the caller start a
    second daemon on a port that is already served, which then fails while
    the state file has already been overwritten — after which nothing can
    reach the daemon that was working perfectly well.
    """
    st = read_state(port)
    if not st:
        return "no"
    r = http(port, "ping", token=st.get("token"), timeout=10)
    if r.get("ok"):
        return "yes"
    return "no" if r.get("_down") else "other"


def pid_alive(pid):
    """Is this pid a live process? (POSIX only — see _pid_alive_win.)"""
    if IS_WIN:
        return _pid_alive_win(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True   # exists, just not ours to signal


def _pid_alive_win(pid):
    """Windows liveness without os.kill.

    CPython's os.kill on Windows has no signal-0 special case: after the
    console-event checks it opens the process for PROCESS_ALL_ACCESS and calls
    TerminateProcess. So `os.kill(pid, 0)` here does not probe the process, it
    *kills* it — which would take down a daemon (and orphan its browser)
    exactly when we are trying to check that it exited cleanly.
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            err = ctypes.get_last_error()
            # Access denied means it exists but belongs to someone else.
            return err == 5
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def profile_locked(profile_dir):
    """True if a live process still holds Chrome's ProcessSingleton.

    This — not the daemon's own pid — is what the next launch actually needs,
    because Chrome outlives the daemon by a moment and keeps the lock.
    """
    try:
        target = os.readlink(os.path.join(profile_dir, "SingletonLock"))
    except OSError:
        return False
    pid = target.rsplit("-", 1)[-1]
    return pid.isdigit() and pid_alive(int(pid))


def clear_stale_profile_lock(profile_dir):
    """Remove Chrome's singleton files when they point at a dead process.

    Chrome normally steals a stale lock, but a leftover ProcessSingleton socket
    makes the relaunch fail outright. Only touches locks whose recorded PID is
    gone, so a profile a live Chrome is using is never disturbed.
    """
    try:
        target = os.readlink(os.path.join(profile_dir, "SingletonLock"))
    except OSError:
        return False
    pid = target.rsplit("-", 1)[-1]
    if not pid.isdigit() or pid_alive(int(pid)):
        return False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(profile_dir, name))
        except OSError:
            pass
    return True


def popen_detached(cmd, env, logfile):
    kw = {}
    if IS_WIN:
        kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                               | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, env=env, stdout=logfile, stderr=logfile,
                            stdin=subprocess.DEVNULL, **kw)


def start(flags, quiet=False):
    port = str(flags.get("port", DEFAULT_PORT))
    state = alive(port)
    if flags.get("force") != "1":
        if state == "yes":
            return {"ok": True, "already_running": True, **read_state(port)}
        if state == "other":
            return {"ok": False, "error":
                    f"something is already listening on 127.0.0.1:{port} but did "
                    f"not accept our token — a daemon from a different BU_HOME, "
                    f"or an unrelated service. Use a different --port, or --force "
                    f"to take the port over."}

    headless = flags.get("headless") == "1"
    wanted = flags.get("channel", "chrome")
    py = ensure_env(force=flags.get("force") == "1",
                    system=flags.get("system") == "1", quiet=quiet, channel=wanted)

    channel = flags.get("channel")
    if channel is None:
        channel = wanted
    # Read back what ensure_env actually installed, always — an explicit
    # `--channel chrome` on a machine with no Chrome falls back to chromium
    # inside ensure_browser, and launching the channel the user asked for
    # instead of the one that exists is a 60s timeout with no stated cause.
    try:
        channel = open(vpy("venv", ".channel")).read().strip() or channel
    except OSError:
        pass

    profile = flags.get("profile", "default")
    profile_dir = vpy("profiles", profile)
    os.makedirs(profile_dir, exist_ok=True)
    os.makedirs(vpy("logs"), exist_ok=True)
    if clear_stale_profile_lock(profile_dir):
        log(f"cleared a stale profile lock left by a previous crash ({profile})", quiet)

    token = os.urandom(16).hex()
    env = dict(os.environ, BU_HOME=HOME, BU_PORT=port, BU_TOKEN=token,
               BU_PROFILE=profile, BU_HEADLESS="1" if headless else "0",
               BU_CHANNEL=("" if channel == "chromium" else channel),
               BU_LEVEL=flags.get("level", "normal"),
               BU_SEED=str(flags.get("seed", "")),
               BU_PROXY=flags.get("proxy", ""),
               BU_BLOCK=flags.get("block", ""),
               BU_LOCALE=flags.get("locale", ""),
               BU_TIMEZONE=flags.get("timezone", ""),
               BU_WINDOW=flags.get("window", "1366x850"),
               BU_DIALOG=flags.get("dialog", "accept"),
               BU_MAX_BODY=str(flags.get("max_body", "20000")),
               BU_CAPTURE_BODIES="1" if flags.get("capture_bodies") == "1" else "0")
    env.pop("PYTHONHOME", None)

    cmd = xvfb_prefix(headless) + [py, os.path.join(HERE, "daemon.py")]
    logf = open(os.path.join(vpy("logs"), f"{port}.log"), "a")

    st = {"port": port, "token": token, "profile": profile, "headless": headless,
          "channel": channel, "level": env["BU_LEVEL"], "log": os.path.join(vpy("logs"), f"{port}.log")}
    try:
        proc = popen_detached(cmd, env, logf)
        st["pid"] = proc.pid
    except Exception as e:
        return {"ok": False, "error": f"could not spawn daemon: {e}"}

    # The state file is written only once the daemon actually answers. Writing
    # it up front (with a token nothing accepts yet) is what turns a slow or
    # failed start into a permanently unreachable daemon.
    for _ in range(120):
        time.sleep(0.5)
        if http(port, "ping", token=token, timeout=5).get("ok"):
            os.makedirs(vpy("state"), exist_ok=True)
            with open(state_path(port), "w") as f:
                json.dump(st, f)
            return {"ok": True, "started": True, **st}
    tail = ""
    try:
        tail = open(st["log"]).read()[-1500:]
    except Exception:
        pass
    out = {"ok": False, "error": "daemon did not come up within 60s",
           "log": st["log"], "pid": st["pid"], "tail": tail}
    hint = _diagnose(tail)
    if hint:
        out["hint"] = hint
    return out


def _diagnose(tail):
    """Turn the daemon log's usual last words into a next step.

    A failed launch otherwise ends at "did not come up within 60s", which says
    nothing about which of half a dozen causes it was.
    """
    low = tail.lower()
    if "missing x server" in low or "no display" in low:
        return ("no display available: run `bu start --headless`, or install "
                "xvfb (apt-get install -y xvfb) for a headful browser")
    if "processsingleton" in low or "singletonlock" in low:
        return ("the profile is locked by another Chrome — close it, or start "
                "with a different --profile")
    if "executable doesn't exist" in low:
        return "the browser binary is missing: rerun `bu start --force`"
    if "address already in use" in low:
        return "the port is taken: pick another --port"
    if "download" in low or "connection" in low or "timed out" in low:
        return "looks like a network problem during the browser download"
    return ""


def stop(port, keep_state=False):
    st = read_state(port)
    reached = False
    if st.get("token"):
        reached = bool(http(port, "close", token=st["token"], timeout=15).get("ok"))
        for _ in range(20):
            time.sleep(0.25)
            if not http(port, "ping", token=st["token"], timeout=3).get("ok"):
                break
    # The daemon closes the browser before exiting, and that takes a moment.
    # Waiting for the process itself means `restart` cannot race the old one
    # for the port, and the profile lock is released before the next launch.
    pid = st.get("pid")
    gone = True
    if pid and pid_alive(pid):
        gone = False
        for _ in range(80):
            time.sleep(0.25)
            if not pid_alive(pid):
                gone = True
                break
    # ...and then wait out Chrome itself, which releases the profile lock a
    # moment after the daemon goes. Without this, an immediate restart races it.
    pdir = vpy("profiles", st.get("profile", ""))
    if st.get("profile") and profile_locked(pdir):
        for _ in range(80):
            time.sleep(0.25)
            if not profile_locked(pdir):
                break
    if not keep_state:
        try:
            os.remove(state_path(port))
        except OSError:
            pass
    # Only believe the shutdown if the port is actually quiet. Reporting
    # "stopped" while a browser is still running (and still holding the
    # profile lock) is worse than reporting nothing: the caller stops looking.
    if not reached and alive(port) != "no":
        return {"ok": False, "port": port,
                "error": f"a daemon is still answering on 127.0.0.1:{port} and "
                         f"would not accept the stored token; it was not stopped",
                "state": st}
    out = {"ok": True, "stopped": gone, "port": port}
    if not gone:
        out["warning"] = (f"daemon pid {pid} did not exit; it may still hold "
                          f"port {port}")
    return out


def doctor(flags):
    port = str(flags.get("port", DEFAULT_PORT))
    py = sys.executable
    daemon_state = alive(port)
    info = {
        "ok": True, "platform": platform.platform(), "python": sys.version.split()[0],
        "python_exe": py, "home": HOME, "port": port,
        "display": os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or "",
        "xvfb": shutil.which("xvfb-run") or "",
        "system_browser": system_chrome() or "",
        "venv": vpy("venv"), "venv_python": venv_python(vpy("venv")),
        "patchright": have_module(venv_python(vpy("venv")), "patchright"),
        "daemon_alive": daemon_state == "yes",
        "profiles": sorted(os.listdir(vpy("profiles"))) if os.path.isdir(vpy("profiles")) else [],
    }
    if info["daemon_alive"]:
        info["daemon"] = http(port, "doctor", token=read_state(port).get("token"))
    info["notes"] = []
    if daemon_state == "other":
        info["notes"].append(
            f"127.0.0.1:{port} is occupied by something that rejects our "
            f"token; use another --port or --force")
    if not info["system_browser"]:
        info["notes"].append("no system Chrome — running bundled Chromium "
                             "(works, slightly easier to fingerprint)")
    if sys.platform.startswith("linux") and not info["display"] and not info["xvfb"]:
        info["notes"].append("headless Linux with no xvfb-run: only `--headless` will start")
    return info


# --------------------------------------------------------------------------
# install into agent runtimes
# --------------------------------------------------------------------------

def install(flags):
    """Register the skill with whatever agent runtimes exist on this machine."""
    quiet = flags.get("quiet") == "1"
    done, bins = [], []

    targets = [
        ("claude-code", os.path.join(os.path.expanduser("~"), ".claude", "skills")),
        ("codex", os.path.join(os.path.expanduser("~"), ".codex", "skills")),
        ("cursor", os.path.join(os.path.expanduser("~"), ".cursor", "skills")),
    ]
    for name, skills_dir in targets:
        parent = os.path.dirname(skills_dir)
        if not os.path.isdir(parent):
            continue
        dest = os.path.join(skills_dir, "browser-undetected")
        os.makedirs(skills_dir, exist_ok=True)
        if os.path.islink(dest):
            if os.path.realpath(dest) == os.path.realpath(ROOT):
                done.append(f"{name}: already linked")
                continue
            # A link is ours to repoint; nothing is lost by doing so.
            os.unlink(dest)
        elif os.path.exists(dest):
            # A real directory here may be a copy someone has edited, so never
            # delete it. Report and leave it alone instead.
            done.append(f"{name}: SKIPPED — {dest} exists and is not a symlink; "
                        f"remove or rename it, then rerun install")
            continue
        try:
            os.symlink(ROOT, dest)
            done.append(f"{name}: linked {dest}")
        except OSError:
            # Windows without developer mode cannot symlink; a copy is the
            # only option, which means edits here will not propagate.
            shutil.copytree(ROOT, dest, dirs_exist_ok=True)
            done.append(f"{name}: copied to {dest} (no symlink permission)")

    bin_dir = os.path.join(HOME, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    names = ["bu", "browser-undetected"]
    for n in names:
        if IS_WIN:
            p = os.path.join(bin_dir, n + ".cmd")
            # newline="" stops the text layer turning \n into \r\n and giving
            # cmd.exe a stray \r to chew on
            with open(p, "w", newline="") as f:
                f.write(f'@echo off\r\n"{sys.executable}" '
                        f'"{os.path.join(HERE, "bu.py")}" %*\r\n')
        else:
            p = os.path.join(bin_dir, n)
            with open(p, "w") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(HERE, "bu.py")}" "$@"\n')
            os.chmod(p, 0o755)
        bins.append(p)

    # Windows PATH comparison is case-insensitive
    on_path = bin_dir.lower() in [p.lower() for p in
                                  os.environ.get("PATH", "").split(os.pathsep)]
    return {"ok": True, "runtimes": done, "launchers": bins,
            "bin_on_path": on_path, "bin_dir": bin_dir,
            "add_to_path": "" if on_path else (
                f'setx PATH "%PATH%;{bin_dir}"' if IS_WIN
                else f'export PATH="{bin_dir}:$PATH"  # add to ~/.bashrc / ~/.zshrc')}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

USAGE = """bu — drive an undetected Patchright browser from any agent

  setup      bu install | doctor | start [--headless] | stop | restart | status

  navigate   bu goto <url> [--wait load|domcontentloaded|networkidle] [--dwell]
             bu back | forward | reload | url | title
  interact   bu click <sel> [--nth N] [--raw] [--clicks 2] [--button right]
             bu clickxy <x> <y> | hover <sel> | drag <sel> <to>
             bu type <sel> <text> [--clear] [--submit] [--typo 0.02]
             bu fill <sel> <text>        (instant value set, no keystrokes)
             bu press <key> [--repeat N] | select <sel> --value v|--label l|--index i
             bu check <sel> | uncheck <sel> | focus <sel> | scroll [px] [--up] [--to sel]
             bu setfiles <sel> <path> | download <sel> [--save path]
  read       bu text [sel] | html [sel] | attr <sel> <name> | value <sel>
             bu count <sel> | exists <sel> | links [--n 200] | eval <js> [--main]
  wait       bu wait <ms> | waitfor <sel> [--state visible|attached] | waiturl <pat>
             bu waitload | waitidle | idle <sec>
  capture    bu screenshot [path] [--full] [--selector sel] | pdf [path]
             bu requests [--n 50] [--filter str] [--method GET] | ws [--filter str]
             bu clear | ua
  session    bu cookies | addcookies <file.json> | setcookies '<json>' | storage
             bu tabs | newtab [url] | tab <i> | closetab [i]
             bu frame <sel> | mainframe | frames
             bu clearcookies | block <regex> | unblock | dialog --mode accept

  any command: --raw (skip humanisation) | --level off|fast|normal|careful

  start flags: --port N --profile NAME --channel chrome|chromium|msedge
               --proxy URL --level off|fast|normal|careful --seed N
               --locale it-IT --timezone Europe/Rome --window 1366x850
               --block REGEX --capture-bodies --max-body N --system --force
"""


def main():
    # On Windows, Python picks the locale codec (cp1252) for a *piped* stdout —
    # which is exactly how an agent captures us — so a page title in Cyrillic
    # or CJK would kill the process mid-print. Force UTF-8 on the way out.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return
    cmd, rest = argv[0], argv[1:]
    pos, flags = parse_args(rest)
    port = str(flags.get("port", DEFAULT_PORT))

    if cmd == "install":
        print(json.dumps(install(flags), indent=1))
        return
    if cmd == "doctor":
        print(json.dumps(doctor(flags), indent=1))
        return
    if cmd == "stop":
        print(json.dumps(stop(port)))
        return
    if cmd == "restart":
        # A restart must not silently swap the profile: without this, restarting
        # a daemon that was started with --profile selftest would come back up on
        # "default" and lose the whole session. Previous settings are the base,
        # explicit flags override them.
        prev = read_state(port)
        inherited = {k: prev[k] for k in ("profile", "channel", "level")
                     if prev.get(k)}
        if prev.get("headless"):
            inherited["headless"] = "1"
        stop(port)
        print(json.dumps(start({**inherited, **flags}), indent=1))
        return
    if cmd == "status":
        st = read_state(port)
        p = http(port, "ping", token=st.get("token"), timeout=10)
        print(json.dumps({"state": st, "alive": bool(p.get("ok")), "daemon": p},
                         indent=1))
        return
    if cmd == "start":
        print(json.dumps(start(flags), indent=1))
        return

    if cmd not in SPEC:
        print(json.dumps({"ok": False, "error": f"unknown command: {cmd}",
                          "hint": "bu --help"}))
        sys.exit(2)

    # any browser command autostarts the daemon on first use
    st = read_state(port)
    if not http(port, "ping", token=st.get("token"), timeout=10).get("ok"):
        r = start(flags)
        if not r.get("ok"):
            print(json.dumps(r, indent=1))
            sys.exit(1)
        st = read_state(port)

    out = http(port, cmd, to_params(cmd, pos, flags), token=st.get("token"))
    print(json.dumps(out, indent=1, ensure_ascii=False))
    if not out.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
