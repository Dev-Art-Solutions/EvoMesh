---
name: validate-skill
description: Check a candidate SKILL.md has valid frontmatter before installing it.
---

A skill can be a group of commands, not only a description -- this one is:
the check below is a real script bundled next to this file, not something to
improvise from prose. Use it before `/skill install` on anything you or
another agent drafted; catching a bad frontmatter block here costs one
command, catching it from a failed install costs a retry with no more detail
than the message.

1. Run `scripts/check.py <path-to-SKILL.md>` with the shell tool (`python`
   must be listed in `harness.shell_allow`).
2. Exit code 0 means it would install; the line printed shows the name and
   description exactly as the registry would read them. A non-zero exit
   names the specific problem -- no frontmatter, unclosed frontmatter,
   invalid YAML, or a missing `name`/`description`.
3. Fix what it reports and run it again. Only call `/skill install` once
   this passes.
