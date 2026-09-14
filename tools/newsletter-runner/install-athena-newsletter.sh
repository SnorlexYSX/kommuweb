#!/usr/bin/env bash
# Install newsletter subscribe API + drip/sync timers on Athena (Linux).
# Usage (on Athena, from tools/newsletter-runner):
#   ./install-athena-newsletter.sh install
#   ./install-athena-newsletter.sh funnel
#   ./install-athena-newsletter.sh status|test-api|test-drip|test-sync|uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PORT="${SUBSCRIBE_API_PORT:-8788}"

usage() {
  cat <<USAGE
Usage: $0 install|funnel|status|test-api|test-drip|test-sync|test-mode-on|test-mode-off|configure-unsubscribe|uninstall

  install        — venv, systemd API + drip + sync timers
  funnel         — expose port $PORT via Tailscale Funnel (public HTTPS)
  status         — systemctl + tailscale funnel status
  test-api       — curl local /health
  test-drip      — run drip sender once
  test-sync      — sync orders tab → Newsletter tab once
  test-mode-on   — drip every 1 minute (NEWSLETTER_TEST_INTERVAL_MINUTES=1)
  test-mode-off  — restore hourly drip + production delays
  configure-unsubscribe — prompt for Apps Script URL + UNSUBSCRIBE_SECRET in .env
USAGE
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1" >&2; exit 1; }
}

venv_python() {
  echo "$ROOT/.venv/bin/python"
}

ensure_env() {
  if [[ ! -f "$ROOT/.env" ]]; then
    echo "Missing $ROOT/.env — copy config.example.env and fill values." >&2
    exit 1
  fi
  for key in GOOGLE_SPREADSHEET_ID GOOGLE_CREDENTIALS_JSON SMTP_HOST SMTP_USER SMTP_PASSWORD MAIL_FROM; do
    grep -qE "^${key}=" "$ROOT/.env" || echo "Warning: .env missing $key" >&2
  done
}

install_venv() {
  need_cmd python3
  need_cmd curl
  if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    # Ubuntu on Athena has no ensurepip/python3-venv package; bootstrap pip manually.
    rm -rf "$ROOT/.venv"
    python3 -m venv "$ROOT/.venv" --without-pip
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip-kommu.py
    "$ROOT/.venv/bin/python" /tmp/get-pip-kommu.py
    rm -f /tmp/get-pip-kommu.py
  fi
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
}

render_unit() {
  local src="$1" dest="$2"
  local py
  py="$(venv_python)"
  sed \
    -e "s|__PROJECT_ROOT__|${ROOT}|g" \
    -e "s|__VENV_PYTHON__|${py}|g" \
    "$src" >"$dest"
}

do_install() {
  ensure_env
  install_venv
  mkdir -p "$ROOT/logs" "$SYSTEMD_DIR"

  for unit in kommu-newsletter-api kommu-newsletter-drip kommu-newsletter-sync; do
    render_unit "$ROOT/systemd/${unit}.service" "$SYSTEMD_DIR/${unit}.service"
  done
  for unit in kommu-newsletter-drip kommu-newsletter-sync; do
    render_unit "$ROOT/systemd/${unit}.timer" "$SYSTEMD_DIR/${unit}.timer"
  done

  systemctl --user daemon-reload
  systemctl --user enable --now kommu-newsletter-api.service
  systemctl --user enable --now kommu-newsletter-drip.timer kommu-newsletter-sync.timer

  echo
  echo "Installed (user systemd in $SYSTEMD_DIR):"
  echo "  kommu-newsletter-api.service  → http://127.0.0.1:${PORT}"
  echo "  kommu-newsletter-drip.timer   → every 1m (welcome); follow-ups 08:00 MYT"
  echo "  kommu-newsletter-sync.timer   → orders → Newsletter every 30m"
  echo
  echo "If timers stop after logout, enable linger: sudo loginctl enable-linger \$USER"
  echo "Next: $0 funnel"
  echo "Then set site newsletter_api_url in kommuweb _config.yml to your Funnel URL."
}

do_funnel() {
  need_cmd tailscale
  if ! curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "API not responding on 127.0.0.1:${PORT} — run: $0 install" >&2
    exit 1
  fi
  echo "Enabling Tailscale Funnel on port ${PORT}…"
  if tailscale funnel --bg "$PORT" 2>/dev/null; then
    :
  else
    sudo tailscale funnel --bg "$PORT"
  fi
  echo
  tailscale funnel status || true
  echo
  echo "Copy the https://… URL above into kommuweb _config.yml:"
  echo "  newsletter_api_url: \"https://YOUR-FUNNEL-HOST/newsletter/subscribe\""
}

do_status() {
  systemctl --user status kommu-newsletter-api.service --no-pager || true
  systemctl --user list-timers 'kommu-newsletter-*' --no-pager || true
  tailscale funnel status 2>/dev/null || echo "(tailscale funnel status unavailable)"
}

do_test_api() {
  curl -sS "http://127.0.0.1:${PORT}/health"
  echo
}

do_test_drip() {
  ensure_env
  "$(venv_python)" "$ROOT/run.py"
}

do_test_sync() {
  ensure_env
  "$(venv_python)" "$ROOT/sync_orders.py"
}

set_env_var() {
  local key="$1" value="$2"
  local file="$ROOT/.env"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    echo "${key}=${value}" >>"$file"
  fi
}

unset_env_var() {
  local key="$1"
  sed -i "/^${key}=/d" "$ROOT/.env"
}

do_test_mode_on() {
  ensure_env
  set_env_var "NEWSLETTER_TEST_INTERVAL_MINUTES" "1"
  cp "$ROOT/systemd/kommu-newsletter-drip.timer" "$ROOT/systemd/kommu-newsletter-drip-production.timer"
  cp "$ROOT/systemd/kommu-newsletter-drip-test.timer" "$SYSTEMD_DIR/kommu-newsletter-drip.timer"
  systemctl --user daemon-reload
  systemctl --user restart kommu-newsletter-drip.timer
  echo "Test mode ON: drip timer every 1 min, 1 min between steps."
  echo "Reset a row to sequence_step=0 to replay the full series."
}

do_test_mode_off() {
  ensure_env
  unset_env_var "NEWSLETTER_TEST_INTERVAL_MINUTES"
  if [[ -f "$ROOT/systemd/kommu-newsletter-drip-production.timer" ]]; then
    cp "$ROOT/systemd/kommu-newsletter-drip-production.timer" "$SYSTEMD_DIR/kommu-newsletter-drip.timer"
  else
    cp "$ROOT/systemd/kommu-newsletter-drip.timer" "$SYSTEMD_DIR/kommu-newsletter-drip.timer"
  fi
  systemctl --user daemon-reload
  systemctl --user restart kommu-newsletter-drip.timer
  echo "Test mode OFF: 1-minute drip timer, follow-ups at 08:00 MYT."
}

do_uninstall() {
  systemctl --user disable --now kommu-newsletter-api.service 2>/dev/null || true
  systemctl --user disable --now kommu-newsletter-drip.timer kommu-newsletter-sync.timer 2>/dev/null || true
  rm -f \
    "$SYSTEMD_DIR/kommu-newsletter-api.service" \
    "$SYSTEMD_DIR/kommu-newsletter-drip.service" "$SYSTEMD_DIR/kommu-newsletter-drip.timer" \
    "$SYSTEMD_DIR/kommu-newsletter-sync.service" "$SYSTEMD_DIR/kommu-newsletter-sync.timer"
  systemctl --user daemon-reload
  if tailscale funnel off 2>/dev/null; then
    :
  else
    sudo tailscale funnel off 2>/dev/null || true
  fi
  echo "Uninstalled newsletter units."
}

do_configure_unsubscribe() {
  exec "$ROOT/configure-unsubscribe.sh"
}

case "${1:-}" in
  install) do_install ;;
  funnel) do_funnel ;;
  status) do_status ;;
  test-api) do_test_api ;;
  test-drip) do_test_drip ;;
  test-sync) do_test_sync ;;
  test-mode-on) do_test_mode_on ;;
  test-mode-off) do_test_mode_off ;;
  configure-unsubscribe) do_configure_unsubscribe ;;
  uninstall) do_uninstall ;;
  *) usage ;;
esac
