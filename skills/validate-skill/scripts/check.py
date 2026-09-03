#!/usr/bin/env python3
"""Check a SKILL.md's frontmatter without installing it.

Mirrors the same rules SkillRegistry.install() enforces, so a pass here
means the install would actually succeed -- not a looser approximation of it.
"""

from __future__ import annotations

import sys

import yaml


def check(text: str) -> tuple[bool, str]:
    if not text.startswith("---"):
        return False, "missing YAML frontmatter (a leading '---' block)"
    end = text.find("\n---", 3)
    if end == -1:
        return False, "frontmatter is opened but never closed with '---'"
    try:
        meta = yaml.safe_load(text[3:end].strip("\n")) or {}
    except yaml.YAMLError as exc:
        return False, f"frontmatter is not valid YAML: {exc}"
    if not isinstance(meta, dict):
        return False, f"frontmatter must be a mapping, not a {type(meta).__name__}"
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        return False, "frontmatter needs both 'name' and 'description'"
    return True, f"ok: name={name!r} description={description!r}"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check.py <path-to-SKILL.md>", file=sys.stderr)
        return 2
    try:
        text = open(sys.argv[1], encoding="utf-8").read()
    except OSError as exc:
        print(f"cannot read {sys.argv[1]}: {exc}", file=sys.stderr)
        return 1
    ok, message = check(text)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
