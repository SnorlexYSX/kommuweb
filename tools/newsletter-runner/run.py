#!/usr/bin/env python3
"""
Kommu newsletter drip runner — self-hosted on your own machine.

Reads subscribers from Google Sheet tab "Newsletter", sends the next email in
the sequence via SMTP, and updates sequence_step / last_sent_at.

Run via systemd on Athena every minute:
  kommu-newsletter-drip.timer → ./run.sh
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

MYT = ZoneInfo("Asia/Kuala_Lumpur")
MYT_DATETIME_FMT = "%d/%m/%Y %H:%M:%S"

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("Install dependencies: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
LOGS = ROOT / "logs"
SEQUENCE_FILE = ROOT / "newsletter_sequence.yaml"
if not SEQUENCE_FILE.exists():
    SEQUENCE_FILE = ROOT.parent.parent / "_data" / "newsletter_sequence.yaml"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Sheet column headers (row 1) — must match docs/newsletter-setup.md
HEADERS = [
    "email",
    "name",
    "source",
    "subscribed_at",
    "sequence_step",
    "last_sent_at",
    "status",
    "next_send_at",
]


def load_env() -> dict:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    required = [
        "GOOGLE_SPREADSHEET_ID",
        "GOOGLE_CREDENTIALS_JSON",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "MAIL_FROM",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"Missing env: {', '.join(missing)}", file=sys.stderr)
        print(f"Copy config.example.env to .env and fill in values.", file=sys.stderr)
        sys.exit(1)
    return os.environ


def load_sequence() -> list[dict]:
    if not SEQUENCE_FILE.exists():
        print(f"Sequence file not found: {SEQUENCE_FILE}", file=sys.stderr)
        sys.exit(1)
    import yaml

    data = yaml.safe_load(SEQUENCE_FILE.read_text())
    return sorted(data, key=lambda x: x["step"])


def get_sheet(env: dict):
    creds_json = env["GOOGLE_CREDENTIALS_JSON"]
    if creds_json.startswith("{"):
        info = json.loads(creds_json)
    else:
        info = json.loads(Path(creds_json).read_text())
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    tab = env.get("NEWSLETTER_SHEET_TAB", "Newsletter")
    return gc.open_by_key(env["GOOGLE_SPREADSHEET_ID"]).worksheet(tab)


def parse_dt(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        for fmt in (MYT_DATETIME_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                tz = MYT if fmt == MYT_DATETIME_FMT else timezone.utc
                return datetime.strptime(s, fmt).replace(tzinfo=tz)
            except ValueError:
                continue
    return None


def wrap_email_html(body: str, email: str = "", step_id: str = "") -> str:
    shell_path = TEMPLATES / "_email_shell.html"
    if not shell_path.exists():
        return body
    from sheet_store import load_dotenv, unsubscribe_url, wrap_tracked_links

    load_dotenv()
    shell = shell_path.read_text().replace("{{content}}", body)
    url = unsubscribe_url(email) if email else "#"
    html = shell.replace("{{unsubscribe_url}}", url)
    if email and step_id:
        html = wrap_tracked_links(html, email, step_id)
    return html


def render_template(step_id: str, name: str, email: str = "") -> tuple[str, str]:
    html_path = TEMPLATES / f"{step_id}.html"
    txt_path = TEMPLATES / f"{step_id}.txt"
    greeting = name.strip() or "there"
    if html_path.exists():
        body = html_path.read_text().replace("{{name}}", greeting)
        html = wrap_email_html(body, email, step_id)
    else:
        html = f"<p>Hi {greeting},</p><p>(Add template: templates/{step_id}.html)</p>"
    if txt_path.exists():
        text = txt_path.read_text().replace("{{name}}", greeting)
    else:
        text = re.sub(r"<[^>]+>", "", html)
    return html, text


def daily_send_path() -> Path:
    LOGS.mkdir(exist_ok=True)
    return LOGS / f"sends-{datetime.now(MYT).strftime('%Y-%m-%d')}.count"


def daily_send_count() -> int:
    path = daily_send_path()
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or "0")
    except ValueError:
        return 0


def record_daily_send() -> int:
    count = daily_send_count() + 1
    daily_send_path().write_text(f"{count}\n")
    return count


def max_sends_per_day(env: dict) -> int:
    raw = (env.get("MAX_SENDS_PER_DAY") or "").strip()
    if not raw:
        return 0
    return int(raw)


def send_email(env: dict, to: str, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env["MAIL_FROM"]
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    port = int(env.get("SMTP_PORT", "587"))
    use_tls = env.get("SMTP_TLS", "true").lower() in ("1", "true", "yes")
    use_ssl = port == 465 or env.get("SMTP_SECURE", "").lower() in ("1", "true", "yes")
    body = msg.as_string()
    if use_ssl:
        with smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=60) as smtp:
            smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            smtp.sendmail(env["MAIL_FROM"], [to], body)
        return
    with smtplib.SMTP(env["SMTP_HOST"], port, timeout=60) as smtp:
        if use_tls:
            smtp.starttls()
        smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        smtp.sendmail(env["MAIL_FROM"], [to], body)


# Follow-up drips go out at 08:00 Malaysia time, not at midnight / the signup hour.
FOLLOW_UP_HOUR_MYT = 8


def format_myt(dt: datetime) -> str:
    return dt.astimezone(MYT).strftime(MYT_DATETIME_FMT)


def follow_up_due_at(last_sent: datetime, delay_days: int) -> datetime:
    """Next drip at 08:00 MYT, `delay_days` calendar days after the last send's date.

    A welcome sent at 01:00 Monday → next email Wednesday 08:00, not 01:00.
    """
    base = last_sent.astimezone(MYT)
    morning = base.replace(hour=FOLLOW_UP_HOUR_MYT, minute=0, second=0, microsecond=0)
    return morning + timedelta(days=delay_days)


def due_at_for_step(
    env: dict,
    step: dict,
    next_step: int,
    subscribed_at: datetime,
    last_sent: datetime | None,
) -> datetime:
    test_minutes = (env.get("NEWSLETTER_TEST_INTERVAL_MINUTES") or "").strip()
    if test_minutes:
        interval = timedelta(minutes=int(test_minutes))
        if next_step == 1:
            return subscribed_at
        base = last_sent or subscribed_at
        return base + interval

    if next_step == 1:
        return subscribed_at

    delay_days = int(step.get("delay_days", 2))
    return follow_up_due_at(last_sent or subscribed_at, delay_days)


def in_follow_up_send_window(now: datetime | None = None) -> bool:
    """Steps 2+ only send during the 08:00 MYT hour, even if the drip timer ran at midnight."""
    current = (now or datetime.now(MYT)).astimezone(MYT)
    return current.hour == FOLLOW_UP_HOUR_MYT


def try_send_first_email(email: str, name: str = "") -> dict:
    """Send welcome (step 1) immediately if Migadu daily quota remains."""
    email = email.strip().lower()
    if not email:
        return {"ok": False, "reason": "invalid_email"}

    env = load_env()
    sequence = load_sequence()
    if not sequence:
        return {"ok": False, "reason": "no_sequence"}

    daily_cap = max_sends_per_day(env)
    if daily_cap and daily_send_count() >= daily_cap:
        return {"ok": False, "reason": "quota"}

    sheet = get_sheet(env)
    rows = sheet.get_all_values()
    col = {h: i + 1 for i, h in enumerate(HEADERS)}
    idx = None
    rec = None
    for i, row in enumerate(rows[1:], start=2):
        candidate = row_to_dict(HEADERS, row)
        if candidate["email"].strip().lower() == email:
            idx, rec = i, candidate
            break
    if rec is None or idx is None:
        return {"ok": False, "reason": "not_found"}
    if rec["status"].strip().lower() in ("unsubscribed", "completed", "inactive"):
        return {"ok": False, "reason": "inactive"}

    step_num = int(rec["sequence_step"] or "0")
    if step_num != 0:
        return {"ok": False, "reason": "already_started", "step": step_num}

    step = sequence[0]
    display_name = name.strip() or rec["name"]
    html, text = render_template(step["id"], display_name, email)
    print(f"Sending step 1 ({step['id']}) immediately to {email}")
    send_email(env, email, step["subject"], html, text)
    today_total = record_daily_send()
    iso_now = datetime.now(MYT).strftime(MYT_DATETIME_FMT)
    new_status = "completed" if 1 >= len(sequence) else "active"
    next_at = ""
    if 1 < len(sequence):
        delay_days = int(sequence[1].get("delay_days", 2))
        next_at = format_myt(follow_up_due_at(datetime.now(MYT), delay_days))
    sheet.update_cell(idx, col["sequence_step"], "1")
    sheet.update_cell(idx, col["last_sent_at"], iso_now)
    sheet.update_cell(idx, col["status"], new_status)
    sheet.update_cell(idx, col["next_send_at"], next_at)
    return {"ok": True, "sent": True, "step": 1, "today_total": today_total}


def row_to_dict(headers: list[str], row: list[str]) -> dict:
    padded = row + [""] * (len(headers) - len(row))
    return dict(zip(headers, padded))


def main() -> None:
    env = load_env()
    sequence = load_sequence()
    sheet = get_sheet(env)
    rows = sheet.get_all_values()
    if not rows:
        print("Sheet is empty")
        return

    headers = [h.strip().lower() for h in rows[0]]
    if headers != HEADERS:
        print(f"Warning: expected headers {HEADERS}, got {headers}")

    now = datetime.now(timezone.utc)
    now_myt = datetime.now(MYT)
    test_mode = bool((env.get("NEWSLETTER_TEST_INTERVAL_MINUTES") or "").strip())
    sent_count = 0
    daily_cap = max_sends_per_day(env)
    already_today = daily_send_count()
    if daily_cap and already_today >= daily_cap:
        print(f"Reached MAX_SENDS_PER_DAY={daily_cap} ({already_today} sent today)")
        print("Done. Sent 0 email(s).")
        return

    due = []
    for idx, row in enumerate(rows[1:], start=2):
        rec = row_to_dict(HEADERS, row)
        email = rec["email"].strip().lower()
        if not email or rec["status"].strip().lower() in ("unsubscribed", "completed", "inactive"):
            continue

        step_num = int(rec["sequence_step"] or "0")
        next_step = step_num + 1
        if next_step > len(sequence):
            continue

        step = sequence[next_step - 1]
        subscribed_at = parse_dt(rec["subscribed_at"]) or now
        last_sent = parse_dt(rec["last_sent_at"])
        due_at = due_at_for_step(env, step, next_step, subscribed_at, last_sent)

        if now < due_at.astimezone(timezone.utc):
            continue

        # Welcome (step 1) can send as soon as quota allows. Later emails wait for 08:00 MYT.
        if next_step > 1 and not test_mode and not in_follow_up_send_window(now_myt):
            continue

        due.append((step_num, due_at, idx, rec, step, next_step))

    # Prefer earlier drip steps when the daily cap will cut the queue short.
    due.sort(key=lambda item: (item[0], item[1], item[2]))

    for step_num, _due_at, idx, rec, step, next_step in due:
        email = rec["email"].strip().lower()
        name = rec["name"]
        html, text = render_template(step["id"], name, email)
        subject = step["subject"]

        if daily_cap and daily_send_count() >= daily_cap:
            print(f"Reached MAX_SENDS_PER_DAY={daily_cap}")
            break

        print(f"Sending step {next_step} ({step['id']}) to {email}")
        send_email(env, email, subject, html, text)
        today_total = record_daily_send()

        iso_now = datetime.now(MYT).strftime(MYT_DATETIME_FMT)
        new_status = "completed" if next_step >= len(sequence) else "active"
        next_at = ""
        if next_step < len(sequence):
            delay_days = int(sequence[next_step].get("delay_days", 2))
            next_at = format_myt(follow_up_due_at(datetime.now(MYT), delay_days))
        col = {h: i + 1 for i, h in enumerate(HEADERS)}
        sheet.update_cell(idx, col["sequence_step"], str(next_step))
        sheet.update_cell(idx, col["last_sent_at"], iso_now)
        sheet.update_cell(idx, col["status"], new_status)
        sheet.update_cell(idx, col["next_send_at"], next_at)
        sent_count += 1

        dry_run_limit = int(env.get("MAX_SENDS_PER_RUN", "50"))
        if sent_count >= dry_run_limit:
            print(f"Reached MAX_SENDS_PER_RUN={dry_run_limit}")
            break
        if daily_cap and today_total >= daily_cap:
            print(f"Reached MAX_SENDS_PER_DAY={daily_cap}")
            break

    print(f"Done. Sent {sent_count} email(s).")


if __name__ == "__main__":
    main()
