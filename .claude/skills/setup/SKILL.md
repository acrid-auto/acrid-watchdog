---
name: setup
description: Interview the person about their company, pager and automations, then write config.json for Acrid Watchdog. Use when they say "set up the watchdog", "add an automation to watch", or "change the pager".
---

# /setup — write config.json by interview

Goal: a `config.json` where every check maps to a real artifact of delivery.

## Steps

1. Ask, in one message, for: company/project name; whether they have a Telegram bot (if not, give the 3-line recipe: message @BotFather → `/newbot` → copy token; message the bot once; open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`).
2. Ask them to list the automations they rely on, one line each, in their words ("the nightly Shopify export", "the Zapier lead sync", "the backup").
3. For EACH automation ask the one question that matters: **"When it works, what exists afterwards that would not exist if it had silently stopped?"** Map the answer to a check type:
   - a file gets written/updated → `file` (path, max_age_hours, min_bytes)
   - a URL/JSON changes → `http` (url; `json_path` + `max_age_hours` if it exposes a timestamp; `contains` otherwise)
   - an n8n workflow runs → `n8n` (workflow id; host + key as `env:` refs)
   - a log line appears → `log` (path, `must_match`, `must_not_match`)
   - anything else they can script → `command`
4. Write `config.json` with `env:VAR` for every secret. Tell them exactly which variables to export and where (shell profile AND the cron environment).
5. Run `python3 watchdog.py check` and paste the output. If a check fails during setup, do not "fix" it by loosening the check — ask whether the automation is actually broken. That is the product working.
6. Run `python3 watchdog.py install` and hand them the cron line.

## Rules
- No secrets in the file. No invented checks. Show the `check` output before saying done.
