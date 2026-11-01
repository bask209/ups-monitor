#!/bin/bash
# Dead-man's switch for the UPS monitor. Runs from a systemd user timer, OUTSIDE the pod, so it still
# works when the whole pod is down (the dashboard cannot report its own death).
#
# "Alive" = /api/health answers at all (200, or 503 when the UPS link is down - the app alerts on that
# itself). Unreachable for FAILS_BEFORE_ALERT consecutive runs => Telegram alert, and try to start the pod
# if systemd has it stopped. When it answers again, send a "back" message with the downtime.
set -u

ENV_FILE="${UPS_ENV:-$HOME/ups-monitor/.env}"
URL="${UPS_HEALTH_URL:-http://127.0.0.1:8330/api/health}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/ups-watchdog"
FAILS_BEFORE_ALERT="${FAILS_BEFORE_ALERT:-3}"
REMIND_EVERY="${REMIND_EVERY:-1800}"   # seconds between repeated "still down" messages
UNIT="${UPS_UNIT:-ups-pod.service}"

mkdir -p "$STATE"
env_get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r'; }
TOKEN="$(env_get TELEGRAM_TOKEN)"
CHAT="$(env_get TELEGRAM_CHAT_ID)"
now=$(date +%s)

rd() { cat "$STATE/$1" 2>/dev/null || echo "${2:-0}"; }
wr() { echo "$2" > "$STATE/$1"; }
dur() { local s=$1; printf '%dh %dm' $((s / 3600)) $((s % 3600 / 60)); }

# Returns 0 only if Telegram accepted the message, so a failed send is retried on the next run.
say() {
    [ -n "$TOKEN" ] && [ -n "$CHAT" ] || { echo "no Telegram credentials in $ENV_FILE" >&2; return 1; }
    curl -fsS -m 15 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=$1" >/dev/null
}

code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$URL" 2>/dev/null)
if [ "$code" = "200" ] || [ "$code" = "503" ]; then
    # Alive. If we had told the user it was down, tell them it is back.
    if [ -f "$STATE/alerted" ]; then
        since=$(rd down_since "$now")
        if say "✅ UPS monitor is back (it was unreachable for $(dur $((now - since))))."; then
            rm -f "$STATE/alerted" "$STATE/fails" "$STATE/down_since" "$STATE/last_remind"
        fi
    else
        rm -f "$STATE/fails" "$STATE/down_since" "$STATE/last_remind"
    fi
    exit 0
fi

# Unreachable.
fails=$(( $(rd fails 0) + 1 ))
wr fails "$fails"
[ -f "$STATE/down_since" ] || wr down_since "$now"
since=$(rd down_since "$now")
[ "$fails" -ge "$FAILS_BEFORE_ALERT" ] || exit 0

# Try to bring it back: start the pod if systemd has it stopped; restart it if it is "active" but not answering.
unit_state=$(systemctl --user is-active "$UNIT" 2>/dev/null)
action="none"
if [ "$unit_state" != "active" ] && [ "$unit_state" != "activating" ]; then
    systemctl --user start "$UNIT" >/dev/null 2>&1 && action="started $UNIT (was $unit_state)"
elif [ "$fails" -ge $((FAILS_BEFORE_ALERT * 2)) ] && [ $((fails % 10)) -eq $((FAILS_BEFORE_ALERT * 2 % 10)) ]; then
    systemctl --user restart "$UNIT" >/dev/null 2>&1 && action="restarted $UNIT (was active but not answering)"
fi

msg="🚨 UPS monitor is NOT responding (down $(dur $((now - since))), http=$code, $UNIT: $unit_state). Recovery action: $action. Power outages are NOT being watched."
if [ ! -f "$STATE/alerted" ]; then
    say "$msg" && { wr alerted 1; wr last_remind "$now"; }
elif [ $((now - $(rd last_remind 0))) -ge "$REMIND_EVERY" ]; then
    say "$msg" && wr last_remind "$now"
fi
exit 0
