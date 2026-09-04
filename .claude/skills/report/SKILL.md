---
name: report
description: Produce the weekly delivery report and summarize what was silently dead. Use on Mondays or when someone asks "is the automation working?" for the week.
---

# /report — the weekly delivery report

1. Run `python3 watchdog.py report --days 7` (writes `reports/delivery-<date>.md`).
2. Lead with the **silently dead the whole window** list. Those are the ones that were probably still showing green somewhere.
3. Then: total runs, failures, and the single automation with the most failures.
4. Offer the file path so they can forward it. Do not editorialize beyond the numbers.
