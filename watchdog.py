#!/usr/bin/env python3
"""Acrid Watchdog — the silent-failure watchdog that runs Acrid Automation,
pointed at your automations.

An automation that fails loudly gets fixed. The dangerous one keeps reporting
success while it quietly stopped delivering: the cron that runs but writes
nothing, the webhook whose token expired, the workflow that is "active" and
hasn't produced a row in four days. This tool asks one question about each of
your automations, on a schedule: did it actually deliver? If not, it pages you,
and it keeps paging until it is fixed. A pager that pages once is a noticer; a
watchdog nags.

Zero dependencies (Python 3.9+ stdlib). One file. One JSON config.

    python3 watchdog.py setup      # interactive: company, pager, automations
    python3 watchdog.py check      # run every check once, page on failures
    python3 watchdog.py status     # what is failing right now, since when
    python3 watchdog.py report     # weekly delivery report -> reports/
    python3 watchdog.py install    # print the cron / launchd line for hourly checks

Check types (config "checks": [...]):
    http     url must answer; optional expect_status, contains, json_path +
             max_age_hours (a timestamp field that must be fresh)
    file     path must exist, be newer than max_age_hours, at least min_bytes
    log      file must exist AND a regex must appear in lines written within
             max_age_hours (default: the file's mtime); optional must_not_match
    n8n      workflow must be active AND its latest execution must be a success
             within max_age_hours (host + api key from env or config)
    command  shell command must exit 0 (put your own probe here)

Pager: Telegram (bot token + chat id). First failure pages at once; it re-pages
every nag_hours until the check passes, then posts one RECOVERED line. A KILL
file in the state dir silences everything (use it during planned maintenance).

Built by an AI that runs a company on 90 scheduled jobs and got burned by every
failure class above. MIT licensed. https://acridautomation.com/watchdog/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("WATCHDOG_CONFIG", HERE / "config.json"))
STATE_DIR = Path(os.environ.get("WATCHDOG_STATE", HERE / "state"))
REPORT_DIR = HERE / "reports"
STATE_FILE = STATE_DIR / "state.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
KILL_FILE = STATE_DIR / "KILL"
VERSION = "1.0.0"
DEFAULT_NAG_HOURS = 20          # once a day, with jitter room
DEFAULT_MAX_AGE_HOURS = 24


# ----------------------------------------------------------------------------- utils
def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hours_ago(dt: datetime | None) -> float:
    return float("inf") if dt is None else (now() - dt).total_seconds() / 3600.0


def env_or(value: str | None, default: str = "") -> str:
    """Config values may be literal or `env:VAR_NAME` so secrets never sit in JSON."""
    if not value:
        return default
    if value.startswith("env:"):
        return os.environ.get(value[4:], default)
    return value


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"[watchdog] no config at {CONFIG_PATH} — run: python3 watchdog.py setup")
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"[watchdog] config is not valid JSON: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"checks": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))


def append_history(row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(row) + "\n")


_SSL_CTX = None


def ssl_context():
    """A verifying TLS context that works on machines whose Python ships without
    a CA bundle (python.org macOS builds until 'Install Certificates.command' is
    run). Tries the default store, then the OS bundles, then certifi. Never
    disables verification — a watchdog that trusts anything is a liability."""
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    import ssl
    candidates = [None, "/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                  "/etc/pki/tls/certs/ca-bundle.crt"]
    try:
        import certifi  # optional, never required
        candidates.append(certifi.where())
    except Exception:
        pass
    for cafile in candidates:
        try:
            ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
            if cafile is None and not ctx.get_ca_certs() and not ctx.cert_store_stats().get("x509", 0):
                continue  # empty default store — try the next bundle
            _SSL_CTX = ctx
            return ctx
        except Exception:
            continue
    _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def http_get(url: str, headers: dict | None = None, timeout: int = 20) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""


def json_path(obj, path: str):
    """Tiny dotted-path getter: 'data.0.updated_at'."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


# ----------------------------------------------------------------------------- checks
def check_http(c: dict) -> tuple[bool, str]:
    url = c["url"]
    headers = {k: env_or(v) for k, v in (c.get("headers") or {}).items()}
    headers.setdefault("User-Agent", f"AcridWatchdog/{VERSION}")
    try:
        status, body = http_get(url, headers, timeout=int(c.get("timeout", 20)))
    except Exception as e:
        return False, f"request failed: {type(e).__name__}: {e}"
    want = int(c.get("expect_status", 200))
    if status != want:
        return False, f"HTTP {status} (expected {want})"
    text = body.decode("utf-8", "replace")
    if c.get("contains") and c["contains"] not in text:
        return False, f"body missing {c['contains']!r}"
    if c.get("json_path"):
        try:
            val = json_path(json.loads(text), c["json_path"])
        except Exception as e:
            return False, f"body is not JSON ({e})"
        if val is None:
            return False, f"json_path {c['json_path']} not found"
        if c.get("max_age_hours"):
            ts = parse_iso(str(val))
            if ts is None:
                return False, f"{c['json_path']}={val!r} is not a timestamp"
            age = hours_ago(ts)
            if age > float(c["max_age_hours"]):
                return False, f"{c['json_path']} is {age:.1f}h old (max {c['max_age_hours']}h)"
            return True, f"HTTP {status}, {c['json_path']} {age:.1f}h fresh"
    return True, f"HTTP {status}"


def check_file(c: dict) -> tuple[bool, str]:
    p = Path(os.path.expanduser(c["path"]))
    if not p.exists():
        return False, f"missing: {p}"
    age = hours_ago(datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc))
    max_age = float(c.get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
    if age > max_age:
        return False, f"stale: last written {age:.1f}h ago (max {max_age}h)"
    size = p.stat().st_size
    if size < int(c.get("min_bytes", 1)):
        return False, f"too small: {size} bytes (min {c.get('min_bytes', 1)})"
    return True, f"fresh ({age:.1f}h), {size:,} bytes"


def check_log(c: dict) -> tuple[bool, str]:
    p = Path(os.path.expanduser(c["path"]))
    if not p.exists():
        return False, f"missing: {p}"
    max_age = float(c.get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
    age = hours_ago(datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc))
    if age > max_age:
        return False, f"log silent for {age:.1f}h (max {max_age}h)"
    tail = p.read_text(errors="replace").splitlines()[-int(c.get("tail_lines", 400)):]
    text = "\n".join(tail)
    if c.get("must_not_match") and re.search(c["must_not_match"], text, re.I | re.M):
        m = re.search(c["must_not_match"], text, re.I | re.M)
        return False, f"error pattern present: {m.group(0)[:80]!r}"
    if c.get("must_match") and not re.search(c["must_match"], text, re.I | re.M):
        return False, f"success pattern {c['must_match']!r} not in last {len(tail)} lines"
    return True, f"active ({age:.1f}h), patterns ok"


def check_n8n(c: dict) -> tuple[bool, str]:
    host = env_or(c.get("host"), os.environ.get("N8N_HOST", "")).rstrip("/")
    key = env_or(c.get("api_key"), os.environ.get("N8N_API_KEY", ""))
    wf = c["workflow_id"]
    if not host or not key:
        return False, "n8n host/api_key missing (set N8N_HOST / N8N_API_KEY or config)"
    h = {"X-N8N-API-KEY": key, "Accept": "application/json"}
    try:
        st, body = http_get(f"{host}/api/v1/workflows/{wf}", h)
        if st != 200:
            return False, f"workflow GET HTTP {st}"
        w = json.loads(body)
        if not w.get("active", False):
            return False, "workflow is INACTIVE"
        st, body = http_get(f"{host}/api/v1/executions?workflowId={wf}&limit=1", h)
        if st != 200:
            return False, f"executions GET HTTP {st}"
        rows = json.loads(body).get("data") or []
    except Exception as e:
        return False, f"n8n API error: {type(e).__name__}: {e}"
    if not rows:
        return False, "no executions at all"
    last = rows[0]
    finished = parse_iso(last.get("stoppedAt") or last.get("startedAt"))
    age = hours_ago(finished)
    max_age = float(c.get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
    status = (last.get("status") or ("success" if last.get("finished") else "unknown")).lower()
    if status not in ("success", "succeeded"):
        return False, f"last execution {status} ({age:.1f}h ago)"
    if age > max_age:
        return False, f"last success {age:.1f}h ago (max {max_age}h)"
    return True, f"active, last success {age:.1f}h ago"


def check_command(c: dict) -> tuple[bool, str]:
    try:
        r = subprocess.run(c["command"], shell=True, capture_output=True, text=True,
                           timeout=int(c.get("timeout", 60)))
    except subprocess.TimeoutExpired:
        return False, "timed out"
    out = (r.stdout + r.stderr).strip().splitlines()
    last = out[-1][:120] if out else ""
    return (r.returncode == 0), (f"exit {r.returncode} {last}".strip())


CHECKS = {"http": check_http, "file": check_file, "log": check_log, "n8n": check_n8n, "command": check_command}


def run_check(c: dict) -> tuple[bool, str]:
    kind = c.get("type")
    fn = CHECKS.get(kind)
    if not fn:
        return False, f"unknown check type {kind!r}"
    try:
        return fn(c)
    except KeyError as e:
        return False, f"config missing field {e}"
    except Exception as e:  # a broken check is a failure, never a silent skip
        return False, f"check crashed: {type(e).__name__}: {e}"


# ----------------------------------------------------------------------------- pager
def page(cfg: dict, text: str) -> bool:
    """Telegram first; stdout always. Returns True if a real page went out."""
    print(text)
    pg = cfg.get("pager") or {}
    token = env_or(pg.get("telegram_bot_token"))
    chat = env_or(pg.get("telegram_chat_id"))
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15, context=ssl_context()) as r:
            return r.status == 200
    except Exception as e:
        print(f"[watchdog] telegram send failed: {e}", file=sys.stderr)
        return False


def nag_due(rec: dict, nag_hours: float) -> bool:
    last = parse_iso(rec.get("last_paged_at"))
    return last is None or hours_ago(last) >= nag_hours


# ----------------------------------------------------------------------------- commands
def cmd_check(args) -> int:
    cfg = load_config()
    state = load_state()
    company = cfg.get("company") or "your company"
    nag_hours = float(cfg.get("nag_hours", DEFAULT_NAG_HOURS))
    if KILL_FILE.exists():
        print(f"[watchdog] KILL file present ({KILL_FILE}) — checks run, pages suppressed")
    failures, recovered, ok = [], [], []
    for c in cfg.get("checks", []):
        name = c.get("name") or c.get("url") or c.get("path") or c.get("workflow_id") or "unnamed"
        passed, detail = run_check(c)
        rec = state["checks"].setdefault(name, {})
        ts = iso(now())
        append_history({"at": ts, "check": name, "ok": passed, "detail": detail})
        if passed:
            if rec.get("failing_since"):
                recovered.append((name, rec["failing_since"]))
                rec.update({"failing_since": None, "last_paged_at": None, "pages": 0})
            rec.update({"last_ok_at": ts, "last_detail": detail})
            ok.append((name, detail))
        else:
            rec.setdefault("failing_since", ts)
            rec.update({"last_detail": detail, "last_fail_at": ts})
            failures.append((name, rec["failing_since"], detail, rec))
    # pages — one message per run, nagging only the ones whose clock is due
    if failures and not KILL_FILE.exists():
        due = [f for f in failures if nag_due(f[3], nag_hours)]
        if due:
            lines = [f"[{company}] {len(failures)} automation(s) not delivering:"]
            for name, since, detail, rec in due:
                age = hours_ago(parse_iso(since))
                lines.append(f"• {name} — {detail} (failing {age:.0f}h, page #{rec.get('pages', 0) + 1})")
            if len(due) < len(failures):
                lines.append(f"(+{len(failures) - len(due)} still failing, nagged earlier)")
            lines.append("Fix it or touch state/KILL to silence. It will keep asking.")
            sent = page(cfg, "\n".join(lines))
            for _, _, _, rec in due:
                rec["last_paged_at"] = iso(now())
                rec["pages"] = rec.get("pages", 0) + 1
                rec["paged_via"] = "telegram" if sent else "stdout"
    for name, since in recovered:
        page(cfg, f"[{company}] RECOVERED: {name} (was failing since {since})")
    save_state(state)
    for name, detail in ok:
        print(f"  ok   {name}: {detail}")
    for name, since, detail, _ in failures:
        print(f"  FAIL {name}: {detail} (since {since})")
    print(f"[watchdog] {len(ok)} ok, {len(failures)} failing, {len(recovered)} recovered — {iso(now())}")
    return 1 if failures else 0


def cmd_status(args) -> int:
    state = load_state()
    rows = state.get("checks", {})
    if not rows:
        print("[watchdog] nothing recorded yet — run: python3 watchdog.py check")
        return 0
    failing = 0
    for name, rec in sorted(rows.items()):
        if rec.get("failing_since"):
            failing += 1
            print(f"  FAIL {name}: {rec.get('last_detail')} — since {rec['failing_since']} (pages {rec.get('pages', 0)})")
        else:
            print(f"  ok   {name}: {rec.get('last_detail')} — last ok {rec.get('last_ok_at')}")
    print(f"[watchdog] {failing} failing of {len(rows)}")
    return 1 if failing else 0


def cmd_report(args) -> int:
    cfg = load_config()
    company = cfg.get("company") or "your company"
    days = int(args.days)
    cutoff = now() - timedelta(days=days)
    rows = []
    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = parse_iso(r.get("at"))
            if at and at >= cutoff:
                rows.append(r)
    by = {}
    for r in rows:
        b = by.setdefault(r["check"], {"runs": 0, "fails": 0, "first_fail": None, "last_detail": ""})
        b["runs"] += 1
        if not r["ok"]:
            b["fails"] += 1
            b["first_fail"] = b["first_fail"] or r["at"]
        b["last_detail"] = r["detail"]
    lines = [f"# {company} — delivery report, last {days} days", "",
             f"_Generated {iso(now())} by Acrid Watchdog {VERSION}. Every line below is a real check, not a status page._", ""]
    if not by:
        lines.append("No checks have run in this window. Run `python3 watchdog.py check` and install the cron.")
    else:
        lines += ["| automation | runs | failed | first failure | last seen |", "|---|---:|---:|---|---|"]
        for name, b in sorted(by.items(), key=lambda kv: -kv[1]["fails"]):
            lines.append(f"| {name} | {b['runs']} | {b['fails']} | {b['first_fail'] or '—'} | {b['last_detail'][:60]} |")
        silent = [n for n, b in by.items() if b["fails"] and b["fails"] == b["runs"]]
        if silent:
            lines += ["", f"**Silently dead the whole window:** {', '.join(silent)}. These were probably still reporting success somewhere."]
    lines += ["", "---",
              "Found something that has been quietly broken for a while? That is exactly the failure class this tool was built on. ",
              "Acrid runs a fixed-scope Silent Failure Audit and a monthly reliability retainer: https://acridautomation.com/audit/"]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"delivery-{now().strftime('%Y-%m-%d')}.md"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n[watchdog] wrote {out}")
    return 0


def cmd_install(args) -> int:
    py = sys.executable or "python3"
    script = HERE / "watchdog.py"
    print("Run the checks every hour. Pick one:\n")
    print("# crontab -e   (Linux / macOS)")
    print(f"5 * * * * cd {HERE} && {py} {script} check >> {HERE}/state/cron.log 2>&1")
    print(f"0 9 * * 1 cd {HERE} && {py} {script} report >> {HERE}/state/cron.log 2>&1   # Monday delivery report\n")
    plist = HERE / "com.acrid.watchdog.plist"
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.acrid.watchdog</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{script}</string><string>check</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>StandardOutPath</key><string>{HERE}/state/launchd.log</string>
  <key>StandardErrorPath</key><string>{HERE}/state/launchd.err</string>
  <key>RunAtLoad</key><true/>
</dict></plist>
""")
    print("# macOS launchd (written next to this file):")
    print(f"cp {plist} ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/{plist.name}")
    return 0


def _ask(prompt: str, default: str = "") -> str:
    s = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return s or default


def cmd_setup(args) -> int:
    print("Acrid Watchdog setup. Answers go to config.json (secrets can stay in env: type env:VAR).\n")
    cfg = load_config() if CONFIG_PATH.exists() else {}
    cfg["company"] = _ask("Company / project name", cfg.get("company", ""))
    pg = cfg.get("pager") or {}
    print("\nPager — Telegram. Create a bot with @BotFather, then message it once and get your chat id from https://api.telegram.org/bot<TOKEN>/getUpdates")
    pg["telegram_bot_token"] = _ask("Telegram bot token (or env:TELEGRAM_BOT_TOKEN)", pg.get("telegram_bot_token", "env:TELEGRAM_BOT_TOKEN"))
    pg["telegram_chat_id"] = _ask("Telegram chat id (or env:TELEGRAM_CHAT_ID)", pg.get("telegram_chat_id", "env:TELEGRAM_CHAT_ID"))
    cfg["pager"] = pg
    cfg["nag_hours"] = float(_ask("Re-page every N hours while something stays broken", str(cfg.get("nag_hours", DEFAULT_NAG_HOURS))))
    checks = cfg.get("checks") or []
    print(f"\nAutomations to watch ({len(checks)} configured). Add one per line; blank name to finish.")
    while True:
        name = _ask("Automation name (blank = done)")
        if not name:
            break
        kind = _ask("Type: http | file | log | n8n | command", "http")
        c: dict = {"name": name, "type": kind}
        if kind == "http":
            c["url"] = _ask("URL that proves it delivered")
            c["expect_status"] = int(_ask("Expected HTTP status", "200"))
            jp = _ask("Optional JSON path to a timestamp field (e.g. data.updated_at)")
            if jp:
                c["json_path"] = jp
                c["max_age_hours"] = float(_ask("Max age of that timestamp, hours", "24"))
        elif kind in ("file", "log"):
            c["path"] = _ask("Path")
            c["max_age_hours"] = float(_ask("Must have been written within N hours", "24"))
            if kind == "log":
                c["must_match"] = _ask("Regex that means success (optional)")
                c["must_not_match"] = _ask("Regex that means failure (optional)", "error|traceback|failed")
        elif kind == "n8n":
            c["host"] = _ask("n8n host (or env:N8N_HOST)", "env:N8N_HOST")
            c["api_key"] = _ask("n8n API key (or env:N8N_API_KEY)", "env:N8N_API_KEY")
            c["workflow_id"] = _ask("Workflow id")
            c["max_age_hours"] = float(_ask("Must have succeeded within N hours", "24"))
        elif kind == "command":
            c["command"] = _ask("Shell command that exits 0 when healthy")
        else:
            print("  unknown type, skipped"); continue
        checks.append(c)
        print(f"  added {name}\n")
    cfg["checks"] = checks
    CONFIG_PATH.write_text(json.dumps(cfg, indent=1))
    print(f"\nWrote {CONFIG_PATH}. Next: python3 watchdog.py check   then   python3 watchdog.py install")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="watchdog", description="Acrid Watchdog — did your automation actually deliver?")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup"); sub.add_parser("check"); sub.add_parser("status"); sub.add_parser("install")
    rp = sub.add_parser("report"); rp.add_argument("--days", default="7")
    a = ap.parse_args(argv)
    return {"setup": cmd_setup, "check": cmd_check, "status": cmd_status, "report": cmd_report, "install": cmd_install}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
