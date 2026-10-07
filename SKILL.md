---
name: browser-undetected
description: Drive a real, undetected Chrome browser (Patchright) from the shell to navigate sites, click, type, fill forms, log in, scrape, screenshot, and capture all network traffic — with human-like mouse curves and typing cadence. The browser stays alive between commands, so multi-step flows work across separate tool calls. Use this whenever a task needs a real browser — opening or interacting with a page, scraping, automating a login, testing a web app, capturing a site's API calls or WebSocket traffic, taking screenshots of a live page, getting past bot detection (Cloudflare, DataDome, Kasada, Akamai, PerimeterX), or when the user says "browser", "patchright", "undetected", "headless", "apri il sito", "scraping", "cattura le richieste", "fai login".
---

# browser-undetected

Pilots a **real Google Chrome** through [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) — a
patched Playwright that removes the leaks automation frameworks normally give
off — and drives it with human-like input instead of teleporting the mouse and
pasting text.

The browser is **long-lived**. One daemon holds it open across every command, so
cookies, tabs, scroll position and JS state survive between tool calls.

## When to use it

Anything where a plain HTTP request is not enough: JavaScript-rendered pages,
logins, form flows, sites behind bot detection, capturing what a page actually
requests, or visually checking a page. If `curl` would do the job, use `curl` —
this is for when it would not.

## Quick start

```bash
bu goto https://example.com                 # daemon autostarts on first use
bu text "h1"                                # read
bu type "#search" "hello world" --submit    # human-speed typing
bu screenshot                               # saved to ./.browser/shot-1.png
```

If `bu` is not on your PATH, call the script directly — the path is stable:

```bash
python3 <this-skill-dir>/scripts/bu.py goto https://example.com
```

First run on a new machine bootstraps itself: creates a virtualenv in
`~/.browser-undetected/venv`, installs Patchright, and picks up a browser. That
takes ~15 s once. Every command prints JSON.

**Always run commands from the project directory you want artifacts in** —
screenshots, the network log and downloads land in `./.browser/`.

## The model

- One daemon per port (default `9317`), holding one persistent browser profile.
- Every command is `bu <verb> [positional args] [--flags]` → JSON on stdout.
- `"ok": true` means it worked. `"ok": false` carries `"error"`, and the exit
  code is 1.
- Screenshots/traffic/downloads go to `<cwd>/.browser/`. Errors from a broken
  selector include the selector that matched nothing — read them, they are
  specific.

Run `bu --help` for the command list, or read `references/commands.md` for the
full reference with every flag and return shape.

## Stealth: what actually matters

The single biggest detection risk is not the driver, it is the *setup*. Patchright
already handles the driver-level leaks (it removes the `Runtime.enable` CDP leak,
strips `--enable-automation`, and adds `--disable-blink-features=AutomationControlled`
itself). What it cannot do for you is behave like a person. So:

**Do:**
- Prefer **headful** (`bu start` defaults to headful; on a headless Linux box it
  starts Xvfb automatically). Headless Chrome is measurably easier to fingerprint.
- Use **a real Chrome** (`channel=chrome`). The bootstrap picks up your system
  Chrome and only falls back to bundled Chromium if there is none. Installing
  Google Chrome is the single highest-value thing you can do for stealth.
- Use a **persistent profile** (default). A profile with history, cookies and a
  warm cache looks like a returning visitor. Fresh profiles on every run look
  like what they are.
- Match the **locale and timezone to the proxy's geography** —
  `bu start --proxy http://user:pass@ip:port --locale en-US --timezone America/Chicago`.
  A UTC clock behind a US IP is a giveaway.
- **Let it be slow.** `bu idle 3` between meaningful actions costs nothing and
  looks human. A page visited and clicked in 200 ms does not.

**Don't:**
- Don't inject a custom `user_agent` or `extra_http_headers` — Patchright's own
  guidance is explicit that fingerprint injection makes you *more* detectable.
- Don't add `--disable-blink-features=AutomationControlled` or `--enable-automation`
  by hand. Patchright manages the Chromium switch list; a duplicated flag is
  itself a signal.
- Don't reach for `--raw` to make a flow faster and then wonder why it got
  blocked. `--raw` exists for speed on pages that don't care, not as a shortcut.

Check what the site sees at any time:

```bash
bu ua    # webdriver flag, plugins, languages, screen, cores, timezone
```

On a healthy setup `webdriver` is `false` and `plugins` is non-zero.

## Piloting it like a human

Humanisation is **on by default** for `click`, `type`, `scroll`, `hover` and
`check`:

- Clicks approach along a bowed Bézier path with hand tremor, hover briefly,
  then press and release with a human-length dwell. It never clicks dead centre.
- Typing runs at a log-normal ~85 ms/character with extra pause on spaces and
  punctuation, occasional hesitation mid-sentence, and a 2% per-character
  chance of a realistic fat-finger typo (an adjacent key) that gets noticed,
  backspaced and retyped.
- Scrolling is a burst of wheel notches with pauses, sometimes with a small
  step back up as if re-reading.

Tuning:

```bash
bu start --level careful          # slower, more hesitant (default: normal)
bu start --level fast             # quicker, still curved
bu type "#q" "text" --typo 0      # no typos (use for passwords/codes)
bu type "#q" "text" --level off   # instant, for a field nobody is watching
bu click "#go" --raw              # raw Playwright click, no humanisation
bu start --seed 1234              # reproducible randomness
```

Use `--raw` (or `--level off`) for the boring 90% of a flow and human speed for
the parts a bot-detector actually watches: the login form, the submit button,
the first click after a page load.

## Recipes

**Log in and keep the session**

```bash
bu goto https://site.com/login --dwell
bu type "#email" "user@example.com"
bu type "#password" "$PASS" --typo 0
bu click "button[type=submit]"
bu waitidle
bu cookies > /tmp/session.json      # the jar as JSON; reload with `bu addcookies /tmp/session.json`
```

**Scrape a JS-rendered page**

```bash
bu goto https://site.com/list --wait networkidle
bu scroll 2000            # trigger lazy loading
bu idle 2                 # let XHRs land
bu text ".item"           # or: bu eval "[...document.querySelectorAll('.item')].map(e=>e.innerText)"
bu links --n 100
```

**Capture a site's API traffic** (the thing DevTools does, but scriptable)

```bash
bu clear
bu goto https://site.com --wait networkidle
bu idle 3
bu requests --filter /api/ --method GET
bu ws --filter wss://            # WebSocket frames, both directions
cat .browser/requests.jsonl      # the complete raw log, one JSON object per line
```

Bodies are not captured by default. For that:
`bu restart --capture-bodies`, then `bu requests --filter /api/`.

**Fill a form the boring way**

```bash
bu fill "#name" "Ada Lovelace"     # instant value set, no keystrokes
bu select "#country" --value IT
bu check "#terms"
bu click "#submit"
bu waitfor ".confirmation"
```

## Gotchas

- **`console.log` does not work.** Patchright disables the Console API to
  remove a CDP leak, so `page.on("console")` and console output are dead. Use
  `bu eval` to return values instead.
- **`eval` runs in an isolated world by default**, so page globals
  (`window.someApp`, `grecaptcha`, framework stores) are invisible. Add
  `--main` to evaluate in the page's own world. DOM APIs, `localStorage` and
  `navigator` are visible either way.
- **Iframes have no human mouse.** After `bu frame <sel>`, interaction falls
  back to element-level clicks inside the frame — still trusted events, just
  without the curved approach. `bu mainframe` to come back.
- **`bu fill` and framework-controlled inputs**: `fill` sets `.value` and fires `input`/`change`
  events. It works with React/Vue, but if a framework ignores it, use `bu type`,
  which produces real key events.
- **PDF output works headful or headless** on current Chrome. If a build ever
  refuses `bu pdf`, restart with `--headless`.
- **A dead daemon is not an error worth debugging.** `bu status` to check;
  `bu restart` to fix. The log is at `~/.browser-undetected/logs/<port>.log`.
- **One daemon serves one project at a time by default.** For parallel work use
  separate ports/profiles: `bu start --port 9318 --profile projectb`.

## Platform notes

| | |
|---|---|
| **Linux (desktop)** | Just works. |
| **Linux (server, no display)** | Headful is still used, under `xvfb-run` automatically. Needs `apt-get install -y xvfb`. Without it, `bu start --headless` works but is weaker. |
| **macOS** | Works. Better with real Chrome in `/Applications`. |
| **Windows** | Works. Blocked in PowerShell? Use `cmd` or `bu.cmd`. |
| **Docker** | `--headless` plus `--disable-dev-shm-usage` (already passed) is the usual setup; add `xvfb` if you can. |

Only Chromium-based browsers are supported — Patchright does not patch Firefox
or WebKit.

## Reference

- `references/commands.md` — every command, flag and return shape.
- `references/stealth.md` — how detection actually works, what Patchright
  patches, and how to tell a real fingerprint problem from a bad flow.
- `scripts/bu.py`, `scripts/daemon.py`, `scripts/human.py` — the implementation,
  if you need to extend it (adding a command is a few lines in two files).
