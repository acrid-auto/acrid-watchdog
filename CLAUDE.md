# Acrid Watchdog — instructions for the agent working in this folder

This folder is a small, single-purpose tool: `watchdog.py` checks whether a
person's automations actually delivered, pages them on Telegram when one did
not, and nags until it is fixed. Zero dependencies. Read `README.md` once.

## Skills in this project

- `/setup` — interview the person and write `config.json`. Use it whenever they
  say "set this up", "add an automation", "change the pager".
- `/watch` — run `python3 watchdog.py check`, read the output, explain each
  failure in one plain sentence and propose the smallest fix.
- `/report` — run `python3 watchdog.py report --days 7`, then summarize the
  silently-dead list first.

## Rules

- Never write a secret into `config.json`. Use the `env:VAR_NAME` form and tell
  the person which environment variables to export (in their shell profile or
  the cron environment).
- Never invent a check. Every check must map to a real artifact of delivery the
  person named: a file, a URL, a workflow id, a log line, a command.
- A check that cannot be verified today is not a check. Run `check` before
  claiming setup is done; show the output.
- Do not add dependencies. The whole point is that this runs anywhere Python 3
  runs, including a cron on a box nobody has touched in a year.
- Keep `state/` out of version control if they commit this folder.
