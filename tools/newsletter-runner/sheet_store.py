"""Google Sheet helpers for KA Inventory → Newsletter tab."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

MYT = ZoneInfo("Asia/Kuala_Lumpur")
MYT_DATETIME_FMT = "%d/%m/%Y %H:%M:%S"

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError as exc:
    raise SystemExit("pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parent

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

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

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ALLOWED_SOURCES = frozenset({"homepage", "checkout", "import", "payment gateway"})


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_credentials():
    load_dotenv()
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")
    if creds_json.startswith("{"):
        info = json.loads(creds_json)
    else:
        info = json.loads(Path(creds_json).read_text())
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_newsletter_sheet():
    load_dotenv()
    sheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SPREADSHEET_ID is not set")
    tab = os.environ.get("NEWSLETTER_SHEET_TAB", "Newsletter")
    gc = gspread.authorize(get_credentials())
    return gc.open_by_key(sheet_id).worksheet(tab)


def subscribed_at_now() -> str:
    """Malaysia time (GMT+8) for new Newsletter rows."""
    return datetime.now(MYT).strftime(MYT_DATETIME_FMT)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_subscribe_payload(body: dict) -> tuple[str, str, str]:
    email = normalize_email(str(body.get("email") or ""))
    if not EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    name = str(body.get("name") or "").strip()
    source = str(body.get("source") or "homepage").strip() or "homepage"
    if source not in ALLOWED_SOURCES:
        raise ValueError("Invalid source")
    return email, name, source


def upsert_subscriber(email: str, name: str = "", source: str = "homepage") -> dict:
    sheet = get_newsletter_sheet()
    rows = sheet.get_all_values()
    col = {h: i + 1 for i, h in enumerate(HEADERS)}

    for idx, row in enumerate(rows[1:], start=2):
        existing = (row[0] if row else "").strip().lower()
        if existing != email:
            continue
        status = (row[6] if len(row) > 6 else "active").strip().lower()
        if name:
            sheet.update_cell(idx, col["name"], name)
        return {
            "ok": True,
            "email": email,
            "created": False,
            "status": status or "active",
        }

    sheet.append_row(
        [email, name, source, subscribed_at_now(), "0", "", "active", ""],
        value_input_option="USER_ENTERED",
    )
    return {"ok": True, "email": email, "created": True, "status": "active"}


def unsubscribe_token(email: str) -> str:
    secret = os.environ.get("UNSUBSCRIBE_SECRET", "").strip()
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), email.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_unsubscribe_token(email: str, token: str) -> bool:
    secret = os.environ.get("UNSUBSCRIBE_SECRET", "").strip()
    if not secret:
        return False
    expected = unsubscribe_token(email)
    return hmac.compare_digest(expected, token.strip())


def unsubscribe_url(email: str) -> str:
    load_dotenv()
    normalized = normalize_email(email)
    base = os.environ.get("NEWSLETTER_APPS_SCRIPT_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "NEWSLETTER_APPS_SCRIPT_URL is not set (same /exec URL as site newsletter_api_url)"
        )
    token = unsubscribe_token(normalized)
    if not token:
        raise RuntimeError("UNSUBSCRIBE_SECRET is not set")
    return (
        f"{base}?action=unsubscribe&email={quote(normalized)}&token={quote(token)}"
    )


BTN_RE = re.compile(r"^[a-z0-9_]{1,40}$")
STEP_RE = re.compile(r"^[a-z0-9_]{1,40}$")
HTTP_RE = re.compile(r"^https?://", re.I)
ANCHOR_RE = re.compile(r"<a\b([^>]*?)>", re.I)


def click_token(email: str, step_id: str, button: str, dest: str) -> str:
    secret = os.environ.get("UNSUBSCRIBE_SECRET", "").strip()
    if not secret:
        return ""
    payload = f"click|{normalize_email(email)}|{step_id}|{button}|{dest}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def click_url(email: str, step_id: str, button: str, dest: str) -> str:
    load_dotenv()
    normalized = normalize_email(email)
    base = os.environ.get("NEWSLETTER_APPS_SCRIPT_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError("NEWSLETTER_APPS_SCRIPT_URL is not set")
    token = click_token(normalized, step_id, button, dest)
    if not token:
        raise RuntimeError("UNSUBSCRIBE_SECRET is not set")
    return (
        f"{base}?action=click"
        f"&email={quote(normalized)}"
        f"&token={quote(token)}"
        f"&step={quote(step_id)}"
        f"&btn={quote(button)}"
        f"&to={quote(dest, safe='')}"
    )


def wrap_tracked_links(html: str, email: str, step_id: str) -> str:
    """Rewrite <a href="..." data-track="btn"> to the Apps Script click redirect."""
    if not email or not step_id or not STEP_RE.match(step_id):
        return html

    def repl(match: re.Match) -> str:
        attrs = match.group(1)
        track = re.search(r"\bdata-track=(['\"])([^'\"]+)\1", attrs, re.I)
        href = re.search(r"\bhref=(['\"])([^'\"]+)\1", attrs, re.I)
        if not track or not href:
            return match.group(0)
        button = track.group(2).strip()
        dest = href.group(2).strip()
        if not BTN_RE.match(button) or not HTTP_RE.match(dest):
            return match.group(0)
        try:
            tracked = click_url(email, step_id, button, dest)
        except RuntimeError:
            return match.group(0)
        new_attrs = re.sub(
            r"\bhref=(['\"])([^'\"]+)\1",
            f'href="{tracked}"',
            attrs,
            count=1,
            flags=re.I,
        )
        return f"<a{new_attrs}>"

    return ANCHOR_RE.sub(repl, html)


def unsubscribe_subscriber(email: str) -> dict:
    email = normalize_email(email)
    sheet = get_newsletter_sheet()
    rows = sheet.get_all_values()
    col = {h: i + 1 for i, h in enumerate(HEADERS)}

    for idx, row in enumerate(rows[1:], start=2):
        existing = (row[0] if row else "").strip().lower()
        if existing != email:
            continue
        sheet.update_cell(idx, col["status"], "inactive")
        return {"ok": True, "email": email, "status": "inactive"}

    return {"ok": False, "error": "Subscriber not found", "email": email}
