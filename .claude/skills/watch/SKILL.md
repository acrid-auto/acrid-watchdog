---
name: watch
description: Run the watchdog checks now and explain every failure in plain language with the smallest fix. Use when they ask "is everything running", "what's failing", or after a page arrives.
---

# /watch — run and explain

1. Run `python3 watchdog.py check` (exit 1 = at least one failure).
2. For each `FAIL` line, write one sentence a non-engineer understands: what was expected, what was found, since when.
3. Propose the smallest plausible fix, ordered by likelihood, using the detail text:
   - `HTTP 401/403` → credential/token expired or rotated
   - `stale: last written Nh ago` → the job did not run, or ran and wrote elsewhere
   - `workflow is INACTIVE` → someone toggled it off (n8n) — ask before re-enabling
   - `last execution error` → open that execution; the failing node is usually the one that talks to a third party
   - `success pattern not in last N lines` → the job runs but its output changed shape
4. If they want it quiet during a planned fix: `touch state/KILL`; remind them to remove it.
5. Never edit a check to make it pass. Never delete history.
