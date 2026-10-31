# v0.5.0-alpha.1 — social copy

Blog: https://blog.devart.solutions/blog/evomesh-0-5-what-we-took-from-codex
Release: https://github.com/Dev-Art-Solutions/EvoMesh/releases/tag/v0.5.0-alpha.1

## X

We read OpenAI's Codex source, then our own logs: 36% of our agents' tool calls were refused, mostly for piping into head. EvoMesh 0.5 runs those scripts the way Codex does. 1,708 of 1,962 refusals now run.
https://blog.devart.solutions/blog/evomesh-0-5-what-we-took-from-codex

(230 characters, counting the link as 23.)

## Facebook

EvoMesh 0.5 is out. We read the source of OpenAI's Codex CLI, but before copying anything we measured our own agents: 501 recent sessions, in which 36% of all tool calls had been refused. Most of them were ordinary terminal habits: piping output into head, joining commands with &&, a find across the whole disk.

So we took the parts the numbers asked for. A script of plain commands now runs as a sequence, each command still checked against the allow-list. Output is cut from the middle, so a test run's verdict survives. An edit that misses only by whitespace lands, as long as exactly one place matches. Replayed against the log, 1,708 of 1,962 refused commands would now run.

The measurement also found a plain bug. Our news agent had never been given its own config file, and 499 of its last 501 jobs went looking for it. That one is fixed too.

Also new: a Crawler with eight ways to fetch a page, a knowledge wiki for every agent, email and MCP behind per-agent grants.
https://blog.devart.solutions/blog/evomesh-0-5-what-we-took-from-codex
