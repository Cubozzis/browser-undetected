#!/usr/bin/env python3
"""Guard the skill's own frontmatter.

An unparseable SKILL.md frontmatter fails silently: the runtime skips the skill
and the agent simply never sees it, with no error anywhere. That happened once
already — a `": "` inside the unquoted `description` scalar ended the scalar and
turned the rest of the line into a nested mapping.

Runs without a browser. Needs PyYAML; if it is missing the structural checks
still run.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FM = re.compile(r"^---\n(.*?)\n---\n", re.S)
FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f": {detail}"))
    if not cond:
        FAILED.append(name)


def main():
    text = open(os.path.join(ROOT, "skills", "browser-undetected", "SKILL.md"),
                encoding="utf-8").read()
    m = FM.match(text)
    check("SKILL.md opens with a frontmatter block", m is not None)
    if not m:
        return finish()

    raw = m.group(1)
    check("frontmatter has no tabs", "\t" not in raw)

    # Structure before parsing: works even with no YAML library.
    keys = [l.split(":", 1)[0] for l in raw.split("\n") if l and not l[0].isspace()]
    check("declares name and description", set(keys) >= {"name", "description"}, keys)
    for line in raw.split("\n"):
        if line.startswith("description:") and ": " in line.split(":", 1)[1]:
            check("description has no bare ': ' (YAML would truncate it)", False,
                  "quote the value or drop the colon")

    try:
        import yaml
    except ImportError:
        print("  skip PyYAML not installed; values unparsed")
        return finish()

    try:
        data = yaml.safe_load(raw)
    except Exception as e:
        check("frontmatter parses as YAML", False, f"{type(e).__name__}: {e}")
        return finish()
    check("frontmatter parses as YAML", True)

    name = data.get("name") or ""
    desc = data.get("description") or ""
    check("name is a short lowercase slug",
          bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)), repr(name))
    check("name within 64 chars", len(name) <= 64, len(name))
    check("description is present", bool(desc.strip()))
    check("description within 1024 chars", len(desc) <= 1024, len(desc))
    check("description says when to use it",
          bool(re.search(r"\buse this\b|\bwhen\b", desc, re.I)))
    return finish()


def finish():
    print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
