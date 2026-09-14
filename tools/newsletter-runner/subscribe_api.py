#!/usr/bin/env python3
"""
Public newsletter subscribe API for Athena + Tailscale Funnel.

  POST /newsletter/subscribe  { "email", "name?", "source?" }
  GET  /health

Listens on SUBSCRIBE_API_PORT (default 8787). Expose with:
  sudo tailscale funnel --bg 8787
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from sheet_store import load_dotenv, upsert_subscriber, validate_subscribe_payload


def _shared_secret() -> str:
    return (os.environ.get("UNSUBSCRIBE_SECRET") or "").strip()


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    secret = _shared_secret()
    if not secret:
        return False
    got = (handler.headers.get("X-Kommu-Secret") or "").strip()
    return got == secret


def _send_first_email(email: str, name: str = "") -> dict:
    from run import try_send_first_email

    return try_send_first_email(email, name)

load_dotenv()

PORT = int(os.environ.get("SUBSCRIBE_API_PORT", "8787"))
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "NEWSLETTER_CORS_ORIGINS",
        "https://kommu.ai,https://www.kommu.ai,https://alexanderyeohsx.github.io",
    ).split(",")
    if o.strip()
]


def cors_origin(request_origin: str | None) -> str:
    if not request_origin:
        return ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else "*"
    if request_origin in ALLOWED_ORIGINS:
        return request_origin
    if request_origin.rstrip("/") in {o.rstrip("/") for o in ALLOWED_ORIGINS}:
        return request_origin
    return ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else ""


class Handler(BaseHTTPRequestHandler):
    server_version = "KommuNewsletterAPI/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _send(self, status: int, payload: dict, origin: str | None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        allow = cors_origin(origin)
        if allow:
            self.send_header("Access-Control-Allow-Origin", allow)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin")
        self.send_response(204)
        allow = cors_origin(origin)
        if allow:
            self.send_header("Access-Control-Allow-Origin", allow)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        origin = self.headers.get("Origin")
        if path in ("/health", "/newsletter/health"):
            self._send(200, {"ok": True}, origin)
            return
        self._send(404, {"error": "Not found"}, origin)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        origin = self.headers.get("Origin")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            self._send(400, {"error": "Invalid request body"}, origin)
            return

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, {"error": "Invalid JSON"}, origin)
            return

        if path in ("/newsletter/send-first", "/send-first"):
            if not _authorized(self):
                self._send(401, {"error": "Unauthorized"}, origin)
                return
            email = str(body.get("email") or "").strip().lower()
            name = str(body.get("name") or "").strip()
            if not email:
                self._send(400, {"error": "Invalid email address"}, origin)
                return
            try:
                result = _send_first_email(email, name)
                self._send(200, result, origin)
            except Exception as exc:
                print(f"send-first error: {exc}")
                self._send(500, {"error": "Internal server error"}, origin)
            return

        if path not in ("/newsletter/subscribe", "/subscribe"):
            self._send(404, {"error": "Not found"}, origin)
            return

        try:
            email, name, source = validate_subscribe_payload(body)
            result = upsert_subscriber(email, name, source)
            if result.get("created"):
                try:
                    sent = _send_first_email(email, name)
                    result["welcome"] = sent
                except Exception as exc:
                    print(f"welcome send error: {exc}")
                    result["welcome"] = {"ok": False, "reason": "send_failed"}
            self._send(200, result, origin)
        except ValueError as exc:
            self._send(400, {"error": str(exc)}, origin)
        except Exception as exc:
            print(f"subscribe error: {exc}")
            self._send(500, {"error": "Internal server error"}, origin)


def main() -> None:
    host = os.environ.get("SUBSCRIBE_API_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, PORT), Handler)
    print(f"Newsletter subscribe API listening on http://{host}:{PORT}")
    print("Expose publicly: sudo tailscale funnel --bg", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
