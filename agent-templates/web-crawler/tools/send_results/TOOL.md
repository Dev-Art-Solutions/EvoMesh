---
name: send_results
description: Save a crawl's results and, if asked, send them as JSON to an API endpoint a human configured by name in config.json. Always saves a copy under results/ first. To give results to another agent, use ask_agent instead.
command: python "{tool_dir}/scripts/send_results.py"
parameters:
  - name: request
    description: >
      JSON: {"task": "<the task, one line>", "results": <what you found: a list
      or object>, "to": ["my-api"]}. "to" names endpoints from config.json's
      "endpoints"; omit it to only save. A raw URL is refused.
    required: true
---

Every call writes results/<time>-<task>.json in the agent's playground. The
POSTed body is {"task", "agent", "sent_at", "results"}.
