# bu — CLI reference

`bu` is a thin HTTP client: it forwards each command to a persistent browser
daemon on `127.0.0.1` and prints the daemon's JSON reply on stdout. Every
command prints JSON; errors are JSON too (`{"ok": false, "error": "..."}`).

Invocation:

```
bu <command> [positional ...] [--flag value | --flag=value | --bool-flag]
```

If `bu` is not on `PATH`, call the script directly:

```
python3 <skill-dir>/scripts/bu.py <command> ...
```

## Syntax rules

- A token starting with `--` is a flag. `--k=v` sets `k=v`; otherwise, if the
  next token does not start with `--`, it is consumed as the value; otherwise
  the flag is set to `"1"`.
- Flag names are mapped to request parameters with `-` turned into `_`
  (`--capture-bodies` → `capture_bodies`). Flags are **not validated**: an
  unknown flag is forwarded as a parameter and ignored by the daemon.
- Boolean flags (never consume the next token): `full`, `raw`, `up`, `dwell`,
  `submit`, `headless`, `force`, `capture-bodies`, `chooser`, `system`,
  `quiet`, `main`, `clear`. Headful is the default, so there is no `--headed` —
  use `--headless` to opt out. A flag that is not in this set and has no
  following token is also set to `"1"`, so an undocumented boolean happens to
  work when it comes last; do not rely on it.
- Positional arguments are bound in the order declared for the command. The
  **last** positional swallows the remaining positional tokens joined by a
  single space, so `bu type "#q" hey there` types `hey there`.
- Commands that take no positional at all (`links`, `ua`, `tabs`, `frames`)
  ignore extra positional tokens. Every command that has a natural single
  argument — including `scroll`, `wait` and `idle` — accepts it positionally;
  the `--flag` form shown alongside is an equivalent alternative.

## Shared flags (every browser command)

| Flag | Default | Meaning |
|---|---|---|
| `--raw` | `0` | Bypass humanisation **for this request only**. Sets the human speed to 0 and switches several commands to direct Playwright calls. Re-evaluated per request, so it never sticks to later commands. |
| `--level` | the daemon's start level (`normal`) | Override the humanisation level for this request: `off` (0.0), `fast` (0.55), `normal` (1.0), `careful` (1.7) — multipliers on the timing model. An unknown value falls back to 1.0. `--raw` wins over `--level`. |
| `--port` | `$BU_PORT` or `9317` | Which daemon to talk to. |

`--raw` bypasses humanisation: clicks, typing, scrolling and hovering are issued
directly instead of through the curved-pointer / log-normal-typing model.

## Setup commands

These are handled by `bu.py` itself and print JSON; they do not need a running
daemon (except `stop`, `restart`, `status`, which use it if present).

### `bu install [--quiet]`

Registers the skill with any agent runtime found and writes `bu` /
`browser-undetected` launchers into `~/.browser-undetected/bin`.

| Flag | Default | Meaning |
|---|---|---|
| `--quiet` | `0` | Suppress the `[bu]` progress lines on stderr. |

Returns:

```json
{"ok": true, "runtimes": ["claude-code: linked ..."], "launchers": ["..."],
 "bin_on_path": false, "bin_dir": "...", "add_to_path": "export PATH=..."}
```

### `bu doctor [--port N]`

Environment report. Returns `ok`, `platform`, `python`, `python_exe`, `home`,
`port`, `display`, `xvfb`, `system_browser`, `venv`, `venv_python`,
`patchright`, `daemon_alive`, `profiles` (list), `notes` (list), and — only
when the daemon is alive — `daemon` (the daemon-side `doctor` object).

### `bu start [flags]`

Bootstraps (venv + Patchright + browser) if needed and spawns the daemon. If a
daemon is already alive on the port and `--force` is not set, it returns
`{"ok": true, "already_running": true, ...state}`.

| Flag | Default | Meaning |
|---|---|---|
| `--port` | `$BU_PORT` or `9317` | Listen port. |
| `--profile` | `default` | Persistent profile directory under `~/.browser-undetected/profiles/`. |
| `--channel` | `chrome` | `chrome` \| `chromium` \| `msedge`. `chrome` uses the system browser; if none is found it falls back to bundled `chromium`. |
| `--headless` | `0` (headful) | Run headless. On headless Linux without a display, headful is wrapped in `xvfb-run` unless this is set. |
| `--level` | `normal` | Default humanisation level: `off` \| `fast` \| `normal` \| `careful`. |
| `--seed` | `""` | Seed for all humanisation randomness (reproducible runs). |
| `--proxy` | `""` | Proxy URL, e.g. `http://user:pass@ip:port`. Adds the WebRTC-IP-leak guard. |
| `--block` | `""` | Regex; matching requests are aborted for the whole session. |
| `--locale` | `""` | Browser locale, e.g. `it-IT`. |
| `--timezone` | `""` | Timezone id, e.g. `Europe/Rome`. |
| `--window` | `1366x850` | Window size (`WxH`). |
| `--dialog` | `accept` | Auto-handler for JS dialogs: `accept` \| `dismiss`. |
| `--capture-bodies` | `0` | Capture response bodies for `xhr`/`fetch`/`document` (JSON/text, under the body-size cap). |
| `--max-body` | `20000` | Cap in bytes on a captured response body. Only meaningful with `--capture-bodies`. |
| `--force` | `0` | Re-run bootstrap / restart even if already running. |
| `--system` | `0` | Use the current interpreter instead of the venv. |

Returns on success: `{"ok": true, "started": true, "port", "token", "profile",
"headless", "channel", "level", "log", "pid"}`. On failure:
`{"ok": false, "error": "daemon did not come up within 60s", "log", "tail"}`.

### `bu stop [--port N]`

Sends `close` to the daemon, waits for it to die, removes the port state file.
Returns `{"ok": true, "stopped": true, "port"}`.

### `bu restart [flags]`

`stop` then `start`, **inheriting the previous run's settings** (profile,
channel, level, headless) and letting any explicit flag override them. So
`bu restart --capture-bodies` keeps the profile the daemon was already using
instead of silently falling back to `default`. Returns `start`'s object.

### `bu status [--port N]`

Returns `{"state": {...state file...}, "alive": true|false, "daemon": {...ping...}}`.

## Lifecycle (daemon verbs)

### `bu ping`

Liveness plus a snapshot. Returns `ok`, `url`, `title`, `headless`, `channel`,
`profile`, `level`, `tabs` (count). If a long command is currently occupying the
browser, `ping` answers immediately from a static snapshot instead of queueing
behind it: it adds `"busy": true` and leaves `url`/`title` empty and `tab`/
`tabs` `null`. Treat `busy` as "alive but not answering questions right now".

### `bu close`

Tells the daemon to exit (it shuts down ~0.2 s after replying). Returns
`{"ok": true, "closing": true}`.

### `bu doctor` (daemon form)

When the daemon is alive, `bu doctor` embeds this object under `"daemon"`. It
returns `ok`, `python`, `executable`, `platform`, `display`, `profile_dir`,
`headless`, `channel`, `level`, `locale`, `timezone`, `proxy` (boolean).

## Navigation

### `bu goto <url> [flags]`

| Arg / Flag | Default | Meaning |
|---|---|---|
| `url` (positional) | `about:blank` | Target URL. |
| `--wait` | `domcontentloaded` | `load` \| `domcontentloaded` \| `networkidle`. |
| `--timeout` | `45000` | ms. |
| `--referer` | none | Referer header. |
| `--dwell` | `0` | After load, idle human-like for `--idle` seconds. |
| `--idle` | `1.5` | Seconds, only used with `--dwell`. |

Returns `{"ok": true, "url", "status", "title"}`. Clears any selected frame.

### `bu back` / `bu forward` / `bu reload` [--timeout N]

`--timeout` default `30000` ms. Returns `{"ok": true, "url", "status"}`. Clears
any selected frame.

### `bu url`

Returns `{"ok": true, "url"}`.

### `bu title`

Returns `{"ok": true, "title", "url"}`.

## Interaction

All interaction commands are humanised unless `--raw` (or `--level off`).

### `bu click <selector> [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `--nth` | `0` | Index among matches; negative counts from the end. |
| `--button` | `left` | `left` \| `right` \| `middle`. |
| `--clicks` | `1` | Click count. |
| `--timeout` | `15000` | ms; read on the `--raw` and in-frame paths, ignored on the humanised one. |

Returns `{"ok": true, "url"}`. Inside a selected frame the click is
element-level (no viewport coordinates to curve through), but `--nth` is still
honoured.

### `bu clickxy <x> <y> [flags]`

| Arg / Flag | Default | Meaning |
|---|---|---|
| `x` (positional) | `0` | Float viewport x. |
| `y` (positional) | `0` | Float viewport y. |
| `--button` | `left` | Mouse button. |
| `--clicks` | `1` | Click count. |

Returns `{"ok": true, "url"}`.

### `bu hover <selector> [--nth N]`

`--nth` default `0`; ignored inside a selected frame. Returns `{"ok": true}`.

### `bu drag <selector> <to>`

`selector` is the drag source, `to` the target selector. Returns
`{"ok": true}`.

### `bu check <selector>` / `bu uncheck <selector>` [--nth N] [--raw]

`--nth` default `0`. The humanised path only clicks when the box's state differs
from what you asked for, so it is safe to repeat. Inside a selected frame the
toggle is element-level, like every other in-frame interaction. Returns
`{"ok": true}`.

### `bu focus <selector>`

Returns `{"ok": true}`.

### `bu select <selector> [--value v | --label l | --index i]`

Precedence: `--label`, then `--index`, then `--value`. Returns
`{"ok": true, "selected": "<resulting value>"}`.

### `bu type <selector> <text> [flags]`

| Arg / Flag | Default | Meaning |
|---|---|---|
| `selector` (positional) | — | Field to focus. Pass `""` to type into the already-focused element (no click). |
| `text` (positional) | `""` | Text; swallows remaining positionals. |
| `--nth` | `0` | Index among matches. |
| `--clear` | `1` when a selector is given, `0` otherwise | Clear the field first. |
| `--submit` | `0` | Press Enter after typing. |
| `--typo` | `0.02` | Per-character chance of a corrected fat-finger typo. `0` disables. |
| `--timeout` | `15000` | ms; read only on the in-frame path (for the focus click). |

Returns `{"ok": true}`. With `--raw`: the field (if any) is emptied, the text
is typed with no human cadence, and `--submit` is **ignored**.
`--nth` is ignored on the in-frame path, which focuses the first match.

### `bu fill <selector> <text> [--timeout N]`

Instant value set (no keystrokes). `--timeout` default `15000` ms. Returns
`{"ok": true}`.

### `bu press <key> [--repeat N]`

`key` positional (default `Enter`); `--repeat` default `1`. Returns
`{"ok": true}`.

### `bu scroll [px] [flags]`

| Arg / Flag | Default | Meaning |
|---|---|---|
| `px` (positional) | `800` | Pixels to scroll. `--px` also works. |
| `--up` | `0` | Scroll upward instead. |
| `--to` | none | Selector; scroll it into view (ignores `px`/`--up`). |

Returns `{"ok": true, "scrollY"}`.

### `bu idle [seconds] [--seconds N]`

Human-like micro-movement for N seconds (positional `seconds`, or `--seconds`,
default `1`). Returns `{"ok": true}`.

### `bu setfiles <selector> <path> [flags]`

| Arg / Flag | Default | Meaning |
|---|---|---|
| `selector` (positional) | `input[type=file]` | File input. |
| `path` (positional) | `""` | One or more paths separated by the OS path separator (`:` on POSIX, `;` on Windows). |
| `--chooser` | `0` | Click the selector and use the native file chooser (for hidden inputs). |
| `--timeout` | `20000` | ms (chooser path). |

Returns `{"ok": true}`.

### `bu download <selector> [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `--save` | `<cwd>/.browser/<suggested_filename>` | Destination path. |
| `--timeout` | `60000` | ms. |

Clicks the selector and waits for the download. Returns
`{"ok": true, "path", "url"}`.

## Reading

| Command | Flags (default) | Returns |
|---|---|---|
| `bu text [selector]` | `selector` default `body`; `--timeout` `15000`; `--max` `12000` | `{"ok": true, "text"}` (truncated text carries `\n...[truncated N chars]`) |
| `bu html [selector]` | no selector → whole document; `--max` `200000` | `{"ok": true, "html"}` |
| `bu attr <selector> <name>` | `name` default `href` | `{"ok": true, "value"}` |
| `bu value <selector>` | — | `{"ok": true, "value"}` |
| `bu count <selector>` | — | `{"ok": true, "count"}` |
| `bu exists <selector>` | — | `{"ok": true, "exists"}` |
| `bu links` | `--n` `200` | `{"ok": true, "links": [{"text", "href"}]}` (text clipped to 120 chars) |
| `bu eval <js>` | `js` default `null`; `--main` `0` | `{"ok": true, "result"}` |

`bu eval` runs in an isolated world by default; `--main` evaluates in the page's
own world (falls back to isolated, adding `"warning"`, on older Patchright).

## Waiting

| Command | Flags (default) | Returns |
|---|---|---|
| `bu wait [ms]` | positional `ms` or `--ms`, default `1000` | `{"ok": true}` |
| `bu waitfor <selector>` | `--state` `visible`; `--timeout` `20000` | `{"ok": true}` |
| `bu waiturl <pattern>` | `pattern` default `**`; `--timeout` `30000` | `{"ok": true, "url"}` |
| `bu waitload` | `--timeout` `45000`; waits for `load` | `{"ok": true}` |
| `bu waitidle` | `--timeout` `45000`; waits for `networkidle` | `{"ok": true}` |

`--state` accepts Playwright states: `attached`, `detached`, `visible`,
`hidden`.

## Capture

### `bu screenshot [path] [flags]`

| Arg / Flag | Default | Meaning |
|---|---|---|
| `path` (positional) | `<cwd>/.browser/shot-N.png` | `N` increments per working directory. |
| `--full` | `0` | Full-page screenshot. |
| `--selector` | none | Screenshot just this element. |

Returns `{"ok": true, "path"}` (absolute).

### `bu pdf [path] [--format A4]`

`path` default `<cwd>/.browser/page.pdf`; `--format` default `A4`. Backgrounds
are always printed. Returns `{"ok": true, "path"}`. Works headful as well as
headless on current Chrome; if your build refuses, restart with `--headless`.

### `bu requests [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `--n` | `50` | Return at most the last N matching events. |
| `--filter` | none | Substring matched against the URL. |
| `--method` | none | HTTP method (upper-cased before matching). |

Filters the in-memory buffer (last 5000 events). Returns `{"ok": true, "count",
"method", "log", "items"}` where `log` is the JSONL path and `items` are raw
events: `{"t": "req", "time", "method", "url", "type", "headers", "post"}` or
`{"t": "res", "time", "status", "url", "type"}` (with `"body"` when
`BU_CAPTURE_BODIES` is on and the body is under the size cap).

### `bu ws [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `--n` | `50` | Return at most the last N frames. |
| `--filter` | none | Substring matched against the WebSocket URL. |

Filters the WebSocket buffer (last 2000 frames). Returns `{"ok": true,
"count", "items"}` with items like `{"t": "ws", "ev": "open|send|recv|close",
"time", "url", "data"}` (binary frames appear as `[binary NB]`).

### `bu clear`

Empties the in-memory request/WebSocket buffers and truncates the current
working directory's `requests.jsonl`. Returns `{"ok": true}`.

## Session, tabs & frames

| Command | Args / Flags (default) | Returns |
|---|---|---|
| `bu cookies [--url U]` | `--url` none | `{"ok": true, "cookies": [...]}` |
| `bu addcookies <path>` | `path` = JSON file | `{"ok": true, "count"}` |
| `bu setcookies [json]` | `json` default `[]` (positional or `--json`) | `{"ok": true, "count"}` |
| `bu clearcookies` | — | `{"ok": true}` |
| `bu storage` | — | `{"ok": true, "local", "session"}` (objects) |
| `bu tabs` | — | `{"ok": true, "tabs": [{"i", "url", "title"}]}` |
| `bu newtab [url]` | `url` optional; `--wait` `domcontentloaded`; `--timeout` `45000`; `--focus` `1` | `{"ok": true, "tabs" (count), "url"}` |
| `bu tab <i>` | `i` default `-1` (last tab) | `{"ok": true, "url", "title"}` |
| `bu closetab [i]` | `i` default `-1` (last tab) | `{"ok": true, "tabs" (count)}` |
| `bu frame [selector]` | `selector` positionals; or `--url PATTERN` | `{"ok": true, "frame"}` or `{"ok": false, "error", "frames"?}` |
| `bu mainframe` | — | `{"ok": true}` |
| `bu frames` | — | `{"ok": true, "frames": [url, ...]}` |
| `bu block <pattern>` | regex | `{"ok": true}` |
| `bu unblock` | — | `{"ok": true}` |
| `bu dialog [--mode accept]` | `--mode` `accept` \| `dismiss` | `{"ok": true, "mode", "last": {"message", "type"}}` |

Notes:

- `addcookies`/`setcookies` normalise Chrome-extension / EditThisCookie exports
  (sameSite, expirationDate, junk keys) as well as Playwright's own format.
  `addcookies` also accepts the wrapper object `bu cookies` prints — a bare
  list or `{"ok": ..., "cookies": [...]}` — so `bu cookies > jar.json` followed
  by `bu addcookies jar.json` round-trips. Anything else raises
  `expected a list of cookies, ...`.
- `bu frame <sel>` selects a frame by iframe selector; without a selector it
  matches the first frame whose URL contains `--url`. All later commands that
  resolve selectors target the frame until `bu mainframe`.
- `bu block` adds a route (repeatable, they stack); `bu unblock` removes **all**
  routes, including the one installed by `start --block`.
- `bu dialog --mode` sets a process-wide auto-handler: future dialogs are
  accepted or dismissed automatically, and the last dialog's message/type are
  reported in `last`.

## Utility

### `bu ua`

Fingerprint readout. Returns `ok`, `ua`, `platform`, `webdriver`, `plugins`
(count), `languages`, `tz`, `cores`, `mem`, `screen` (`[width, height,
availWidth, devicePixelRatio]`).

## Working directory & artifacts

`bu` attaches the caller's current directory to every request (`wd=<cwd>`), and
the daemon writes all artifacts relative to it — so **run commands from the
project directory the work belongs to**.

| Path | Written by |
|---|---|
| `<cwd>/.browser/requests.jsonl` | Every request/response/WebSocket event, appended as one JSON object per line, for the lifetime of the daemon. |
| `<cwd>/.browser/shot-N.png` | `screenshot` when no path is given (`N` starts at 1 and increments per working directory). |
| `<cwd>/.browser/page.pdf` | `pdf` when no path is given. |
| `<cwd>/.browser/<name>` | `download` when `--save` is not given (suggested filename). |

An explicit `path`/`--save` is used as-is (its parent directories are created),
and the absolute path is returned in the JSON.

## Multiple daemons

- `--port` selects the daemon. Each port has its own state file at
  `~/.browser-undetected/state/<port>.json` and log at
  `~/.browser-undetected/logs/<port>.log`. Different ports are fully
  independent browsers.
- `--profile` selects the persistent browser profile
  (`~/.browser-undetected/profiles/<name>`), holding cookies, history and cache.
  It is read at `start`/`restart` time (it becomes `BU_PROFILE`), so passing
  `--profile` to a browser command only matters when that command autostarts the
  daemon.
- Two projects sharing a port share one browser; run
  `bu start --port 9318 --profile projectb` for a second, isolated one.

## Environment variables

| Variable | Read by | Default | Meaning |
|---|---|---|---|
| `BU_HOME` | `bu.py` (and the daemon) | `~/.browser-undetected` | Root for venv, profiles, state, logs, bin. |
| `BU_PORT` | `bu.py` | `9317` | Default daemon port. |
| `PLAYWRIGHT_BROWSERS_PATH` | Patchright/Playwright itself | — | Browser-binary location. Not referenced by `bu.py`/`daemon.py`; honoured only by the underlying Playwright library. |
| `DISPLAY` / `WAYLAND_DISPLAY` | `bu.py` | — | Used to decide whether to wrap headful in `xvfb-run`. |

`bu start` injects these into the daemon's environment (all prefixed `BU_`):

| Variable | Source | Default |
|---|---|---|
| `BU_HOME` | `BU_HOME` env | `~/.browser-undetected` |
| `BU_PORT` | `--port` | `9317` |
| `BU_TOKEN` | random per start | — (required on every request) |
| `BU_PROFILE` | `--profile` | `default` |
| `BU_HEADLESS` | `--headless` | `0` |
| `BU_CHANNEL` | `--channel` (empty for `chromium`) | `chrome` |
| `BU_LEVEL` | `--level` | `normal` |
| `BU_SEED` | `--seed` | `""` |
| `BU_PROXY` | `--proxy` | `""` |
| `BU_BLOCK` | `--block` | `""` |
| `BU_LOCALE` | `--locale` | `""` |
| `BU_TIMEZONE` | `--timezone` | `""` |
| `BU_WINDOW` | `--window` | `1366x850` |
| `BU_DIALOG` | `--dialog` | `accept` |
| `BU_CAPTURE_BODIES` | `--capture-bodies` | `0` |
| `BU_MAX_BODY` | `--max-body` | `20000` |

`bu.py` builds the daemon environment as `dict(os.environ, ...)` and then sets
every `BU_*` above explicitly, so exporting `BU_MAX_BODY` (or any other `BU_*`
in this table) in your shell does **not** reach the daemon — the flag is the
only way in. `PYTHONHOME` is stripped from the daemon environment.

## Humanisation

Levels (`--level`, or `--level` at `start`) map to a speed multiplier applied to
the timing model:

| Level | Speed |
|---|---|
| `off` | 0.0 |
| `fast` | 0.55 |
| `normal` | 1.0 |
| `careful` | 1.7 |

With a positive speed: the pointer travels a bowed Bézier path with Gaussian
tremor at a Fitts's-law duration, clicks hover before committing and never aim
dead centre; typing follows a log-normal per-character cadence (base ~85 ms,
extra pause on spaces/punctuation/uppercase) with an occasional mid-word
hesitation and, at `typo_rate`, a neighbouring-key typo that is noticed,
backspaced and retyped; scrolling is a burst of 2–9 wheel notches with
0.02–0.15 s gaps and an occasional small step back. `typo_rate` defaults to
`0.02` (`--typo`), and `--seed` at `start` makes the whole model reproducible.

## Exit codes

| Code | When |
|---|---|
| `0` | The command succeeded (daemon returned `ok: true`). |
| `1` | The daemon returned `ok: false`, or the daemon could not be autostarted. |
| `2` | Unknown command (not in the command table and not a setup verb). |

Setup verbs (`install`, `doctor`, `start`, `stop`, `restart`, `status`) always
exit `0`; check the `ok` field in their JSON.

## Error shape

Errors carry `ok: false` and an `error` string; the daemon also returns HTTP
400. A selector that matches nothing produces a message naming the selector
(e.g. `selector matched nothing: '#foo'`). Autostart failures add `log` and
`tail`.
