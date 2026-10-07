# browser-undetected — agent entry point

This repository provides a skill that lets you pilot a real, undetected Chrome
browser from the shell.

**Read [`skills/browser-undetected/SKILL.md`](skills/browser-undetected/SKILL.md)
first.** It is the complete instruction set: when to use this, how the daemon
model works, the stealth rules that actually matter, and recipes for
login/scrape/traffic-capture flows.

## Invoking it

```bash
python3 skills/browser-undetected/scripts/bu.py <command> [args] [--flags]
```

or `bu <command> ...` if `.../scripts/bu.py install` has put the launcher on your
PATH. Every command prints JSON on stdout; `"ok": false` carries an `"error"`
and exits 1.

If the skill is installed into an agent runtime, the equivalent path is
`~/.claude/skills/browser-undetected/scripts/bu.py` (or `~/.codex/skills/...`,
`~/.cursor/skills/...`).

## The one rule worth repeating

Run commands **from the project directory the work belongs to**. The browser is
long-lived and shared, but artifacts — screenshots, `requests.jsonl`,
downloads — are written to `./.browser/` relative to your current directory.

## Everything else

- Full command reference: [`references/commands.md`](skills/browser-undetected/references/commands.md)
- Detection and troubleshooting: [`references/stealth.md`](skills/browser-undetected/references/stealth.md)
- Help: `python3 skills/browser-undetected/scripts/bu.py --help`
