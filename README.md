# browser-undetected

A skill that gives any coding agent — Claude Code, Codex, Cursor, Aider, or you
at a shell — a **real, undetected Chrome** it can pilot like a human.

Built on [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python),
the patched-Playwright fork that removes the CDP leaks bot-detectors look for.
On top of that this adds the part a patched driver cannot give you: a browser
that *behaves* like someone is sitting in front of it.

```bash
bu goto https://example.com
bu type "#search" "hello" --submit     # ~85ms/char, curved mouse, real key events
bu requests --filter /api/             # everything the page fetched
```

## What it does

- **Undetected by default.** Real Chrome (not bundled Chromium where possible),
  persistent profile, headful, no fingerprint injection, no duplicated
  automation flags. `navigator.webdriver` is `false`, plugins are present, the
  screen size is real.
- **Human input.** Mouse moves along a bowed Bézier path with hand tremor and a
  Fitts's-law duration; clicks hover before committing; typing has a log-normal
  cadence with realistic, self-corrected typos; scrolling eases in bursts.
- **A browser that stays alive.** A daemon holds it open between commands, so
  logins, tabs and page state survive across tool calls.
- **Full traffic capture.** Every request, response and WebSocket frame is
  logged to `.browser/requests.jsonl` as it happens.
- **Zero-setup on a new machine.** First run creates its own virtualenv,
  installs Patchright and finds a browser. Windows, macOS, Linux.
- **Works on a headless server.** Starts Xvfb by itself rather than falling
  back to detectable headless mode.

## Install

```bash
cd browser-undetected          # this folder, however you got it
python3 scripts/bu.py install  # registers the skill with any agent runtime it finds
python3 scripts/bu.py doctor   # check the environment
```

`install` symlinks the skill into `~/.claude/skills/`, `~/.codex/skills/` and
`~/.cursor/skills/` when those exist, and drops `bu` / `browser-undetected`
launchers in `~/.browser-undetected/bin`. It prints the PATH line to add if
needed — until you export that, call `python3 scripts/bu.py <command>` instead
of `bu <command>`.

There is nothing else to install. The first browser command bootstraps the rest:

```bash
bu goto https://example.com      # or: python3 scripts/bu.py goto https://example.com
```

**For the strongest fingerprint install Google Chrome first.** Patchright's own
guidance is to use a branded Chrome; the bootstrap uses it automatically when
it finds one and falls back to bundled Chromium when it does not. `bu doctor`
tells you which you are running.

## Commands

```
navigate   goto, back, forward, reload, url, title
interact   click, clickxy, hover, drag, type, fill, press, select,
           check, uncheck, focus, scroll, setfiles, download
read       text, html, attr, value, count, exists, links, eval
wait       wait, waitfor, waiturl, waitload, waitidle, idle
capture    screenshot, pdf, requests, ws, clear, ua
session    cookies, addcookies, setcookies, clearcookies, storage, tabs,
           newtab, tab, closetab, frame, mainframe, frames, block, unblock,
           dialog
lifecycle  ping, close
setup      install, doctor, start, stop, restart, status
```

Every command prints JSON. Full reference with all flags and return shapes:
[`references/commands.md`](references/commands.md).

## Humanisation

On by default for clicks, typing, scrolling and hovering.

```bash
bu start --level careful     # slower, more hesitant
bu start --level fast        # still curved, just brisker
bu type "#q" "text" --typo 0 # no typos
bu click "#go" --raw         # bypass entirely
bu start --seed 42           # reproducible randomness
```

See [`references/stealth.md`](references/stealth.md) for what actually gets you
detected, what Patchright patches for you, and what it cannot.

## Platforms

| | |
|---|---|
| Linux desktop | native |
| Linux server | automatic Xvfb (needs `xvfb` installed) |
| macOS | native |
| Windows | native, `bu.cmd` launcher included |
| Docker | `--headless`, or Xvfb for headful |

Chromium-based browsers only — Patchright does not patch Firefox or WebKit.

## Layout

```
SKILL.md                 agent-facing instructions (what to read first)
AGENTS.md                entry point for non-Claude agents
references/commands.md   full CLI reference
references/stealth.md    detection, fingerprints, troubleshooting
scripts/bu.py            CLI + bootstrap
scripts/daemon.py        persistent browser daemon
scripts/human.py         human input model
tests/                   offline unit checks + live end-to-end smoke test
```

Adding a command means a few lines in `bu.py` (the `SPEC` table) and a branch in
`daemon.py`'s `handle()`.

## Tests

```bash
python3 tests/test_human.py        # offline, no browser, no deps
python3 tests/test_frontmatter.py  # offline; guards the skill's own YAML header
python3 tests/smoke.py             # live: boots a browser, drives a real form
```

## Credits

[Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) by
Kaliiiiiiiiii-Vinyzu, itself a patched
[Playwright](https://playwright.dev/python/). This project is the orchestration,
the human-input model and the packaging around it.

## License

MIT
