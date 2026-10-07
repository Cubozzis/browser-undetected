# Detection, fingerprints, and what to do about them

Practical notes for when a page blocks you, and for understanding what this
tool does and does not solve. Written against Patchright 1.63 (which bundles
its own patched Playwright).

## The layers, in the order they actually get you

Bot detection is not one check. Ranked roughly by how often each is the real
reason a session dies:

1. **IP reputation.** A datacenter IP is a datacenter IP. No amount of
   fingerprint work fixes this — it is the most common cause by a wide margin,
   and it is the one this tool cannot help with. Use a residential or mobile
   proxy if the target is serious.
2. **Behavior.** No mouse movement at all, a form filled in 60 ms, a scroll
   that teleports, a click at the exact centre of every element, perfectly
   regular keystroke intervals. This is what `human.py` exists for.
3. **CDP / driver artifacts.** `navigator.webdriver`, the `Runtime.enable`
   leak, automation switches in the command line. **Patchright handles this
   layer** — it is the entire point of the fork.
4. **Profile and history.** A brand-new profile with no cookies, no
   localStorage and an empty history reads as a fresh identity every time.
   A persistent profile that comes back looks like a returning visitor.
5. **Browser fingerprint.** Canvas/WebGL/audio hashes, fonts, `navigator.*`
   oddities, screen geometry, timezone vs IP mismatch.
6. **TLS/JA3 fingerprint.** A Python HTTP client's TLS handshake differs from
   Chrome's. Patchright drives a real Chrome, so this layer is inherently fine
   — it only bites you if you mix raw HTTP calls into the same flow.

## What Patchright patches

From the project's own documentation:

- **`Runtime.enable` leak** — the big one. Playwright enables the Chrome
  DevTools Protocol Runtime domain, which is observable from the page.
  Patchright executes JS in isolated execution contexts instead.
- **`Console.enable` leak** — patched by disabling the Console API entirely.
  *Consequence: `console.log` does not work and `page.on("console")` is dead.*
- **Command-line switch leaks.** Patchright rewrites Playwright's default
  Chromium arguments: it **adds** `--disable-blink-features=AutomationControlled`
  and **removes** `--enable-automation`, `--disable-popup-blocking`,
  `--disable-component-update`, `--disable-default-apps` and
  `--disable-extensions`.
- **Closed shadow roots** — normal locators pierce them.
- Assorted Playwright setup leaks.

**This is why you must not add those flags yourself.** Passing
`--disable-blink-features=AutomationControlled` or `--enable-automation` by hand,
or overriding `ignore_default_args` (which would put `--disable-extensions`
back), undoes part of the patch. `scripts/daemon.py` deliberately passes almost
no arguments for this reason.

## The setup that matters

Patchright's own recommended configuration, which this tool implements:

```python
chromium.launch_persistent_context(
    user_data_dir=...,   # persistent profile: history, cookies, warm cache
    channel="chrome",    # a real branded Chrome, not bundled Chromium
    headless=False,      # headful; on a headless server, run under Xvfb
    no_viewport=True,    # viewport follows the real window size
    # and NO custom user_agent, NO extra_http_headers
)
```

Two of these are counter-intuitive and worth stating plainly:

- **Do not set a `user_agent` or custom headers.** The instinct is to spoof;
  the effect is to create an inconsistency between the UA string and everything
  else the browser reports. Patchright's guidance is explicit: use Chrome
  without fingerprint injection.
- **Headless is weaker.** `headless=True` without a channel launches
  `chromium-headless-shell`, which is a different binary with a different
  fingerprint from the Chrome your users run. On Linux servers this tool
  starts `xvfb-run` automatically rather than accepting that downgrade.

**Locale and timezone must match the proxy.** A browser reporting
`Europe/Rome` behind a US IP is a mismatch that costs nothing to fix:

```bash
bu start --proxy http://user:pass@1.2.3.4:8080 --locale en-US --timezone America/Chicago
```

**WebRTC leaks the real IP even through a proxy** unless you disable
non-proxied UDP. The daemon adds
`--webrtc-ip-handling-policy=disable_non_proxied_udp` automatically whenever a
proxy is configured.

**WebGL needs a flag on a GPU-less box.** Since Chrome 137 the software WebGL
fallback is refused unless `--enable-unsafe-swiftshader` is passed, so on any
VPS or container without a GPU `getContext("webgl")` returns `null`. That is a
loud headless tell — real Chrome almost always has WebGL — and it silently
breaks every WebGL-using site. The daemon detects the absence of `/dev/dri` and
passes the flag automatically. The flag only *permits* SwiftShader; where a
real GPU exists Chrome still renders in hardware.

The residue is that the renderer string on such a machine reads
`ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device ...), SwiftShader driver)`.
Fingerprint services score that as a software canvas. It is a milder tell than
having no WebGL at all, and it is the honest ceiling of what a driver-level
patch can do: only a fingerprint patch (or a real GPU) changes the string.

## What Patchright does not do

Be clear-eyed about the boundary. Patchright is a driver-level patch; it is not
an anti-detect browser:

- **No fingerprint spoofing.** No canvas, WebGL, audio, WebGPU or font
  hardening. What your machine reports is what the page sees.
- **No TLS/JA3 impersonation.**
- **No proxy/VPN of its own,** and nothing to repair a bad IP.
- **No behavioral layer** — that part is `human.py`'s job.

Its published pass-list against Cloudflare, DataDome, Kasada, Akamai, Shape and
others is a **project claim without published methodology**. Treat it as a
signal that the driver layer is solid, not as a guarantee about your specific
target, IP and flow.

## The behavioral layer

What `human.py` does, and why each part matters:

| Signal | Naive automation | Here |
|---|---|---|
| Mouse path | teleports between points | cubic Bézier bowed off the chord, with hand tremor |
| Mouse timing | constant per-step delay | Fitts's-law duration, bell-shaped velocity profile |
| Click | instant at the element centre | sample point inside the box, hover, then press with dwell |
| Typing | all characters at once | log-normal ~85 ms/char, longer after spaces |
| Typos | none, ever | 2% per char, adjacent QWERTY key, noticed and corrected |
| Scrolling | one 800 px jump | burst of eased wheel notches with pauses |
| Waiting | exact fixed sleep | `bu idle` micro-movements and drift |

The single most effective knob is not any of those: it is **not being in a
hurry**. `bu idle 3` after landing on a page costs three seconds and removes
the strongest behavioral signal there is.

Tuning lives in `bu start --level` and per-command `--level` / `--raw`. Use
`--raw` for the parts nobody watches and human pacing for the parts they do.

## Gotchas that look like detection but are not

- **`bu eval` returns `null` for a page global.** `evaluate` runs in an
  isolated world by default, so `window.someApp` is invisible. That is
  intended, not a bug — add `--main` when you need the page's own world.
- **`console.log` produces nothing.** Patchright disables the Console API.
- **A framework ignores `bu fill`.** `fill` sets `.value` and dispatches
  `input`/`change`; React and Vue usually accept that, but if a component
  swallows it, use `bu type` for real key events.
- **AJAX never fires in your headless flow.** Almost always a timing problem:
  add `bu waitidle` or `bu idle 2` and re-check `bu requests`.

## Measured, not claimed

Typical output on a GPU-less Linux VPS with no proxy, headful under Xvfb,
`channel=chrome`. Run these yourself against your own IP — the scores move with
the IP far more than with anything in this repo.

| Test | Result |
| --- | --- |
| `bot.sannysoft.com` | 30 rows passed, 1 marked: WebGL renderer reads SwiftShader |
| `navigator.webdriver` | present and `=== false` (the patched driver, not a deleted property) |
| `navigator.plugins` | 5 entries, real `PluginArray` |
| Chrome-only globals (`window.chrome`) | present |
| Permissions API | `prompt` (headless usually reports `denied`) |
| `abrahamjuliot.github.io/creepjs` | grade **C — "moderate"** |

Read that table for what it is: the *driver* layer is clean, and the residue is
the software renderer plus the datacenter IP. CreepJS grading a VPS Chrome as
"moderate" rather than "trusted" is the expected result and is not a bug you
can fix from inside the browser.

## When a site still blocks you

Work down this list — it is ordered by how often each item is the answer.

1. **Check the IP.** `bu ua` and view the site's own IP-echo endpoint, or just
   try the same flow through a different proxy. Datacenter IPs fail first.
2. **Check the fingerprint.** `bu ua` should show `webdriver: false`, a
   non-zero `plugins` count, a plausible screen size and a timezone matching
   the IP.
3. **Check you are not headless.** `bu status` reports `headless` and
   `channel`. If it says `headless: true`, restart without `--headless` (and
   install `xvfb` on a server).
4. **Check the channel.** If it says `chromium`, you are on bundled Chromium.
   Install Google Chrome and `bu start --force --channel chrome`.
5. **Check the profile.** A fresh profile on every run is a red flag. Let the
   profile persist, or seed it (`bu addcookies`).
6. **Slow the flow down.** `bu start --level careful`, add `bu idle` between
   steps, and replace any `--raw` click in a sensitive flow with a human one.
7. **Read the page.** `bu text body`, `bu html`, `bu screenshot` — the block
   page usually names the vendor, and the vendor tells you which layer failed.
8. **Capture the traffic.** `bu requests --filter challenge` and `bu ws` show
   whether a challenge request was made and what came back.

If 1–3 are clean and you are still blocked, the answer is usually the IP, not
the browser.
