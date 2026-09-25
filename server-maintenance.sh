#!/usr/bin/env bash
#
# Server maintenance: cap systemd-journald size, clean apt cruft, and
# install/configure fail2ban for sshd.
# ---------------------------------------------------------------------
# Generic and idempotent — safe to copy to any Ubuntu/Debian server and
# re-run any time (e.g. as a monthly cron job).
#
# Usage (needs root):
#     sudo ./server-maintenance.sh
#
# Config can be overridden with environment variables, e.g.:
#     JOURNAL_MAX_USE=500M SSHD_MAXRETRY=3 sudo -E ./server-maintenance.sh
#
#     JOURNAL_MAX_USE       Cap for total journal disk use   (default: 300M)
#     JOURNAL_MAX_FILE_SIZE Cap per journal file             (default: 50M)
#     SSHD_PORT             Port(s) fail2ban watches for ssh (default: ssh)
#     SSHD_MAXRETRY         Failed attempts before a ban     (default: 5)
#     SSHD_FINDTIME         Window (seconds) attempts count  (default: 600)
#     SSHD_BANTIME          Initial ban length (seconds)     (default: 3600)
#     SKIP_JOURNAL=1        Skip the journald step
#     SKIP_APT=1            Skip the apt cleanup step
#     SKIP_FAIL2BAN=1       Skip the fail2ban step
#     FORCE_JAIL_CONFIG=1   Overwrite an existing jail.local not managed
#                           by this script (default: leave it alone)
#
set -euo pipefail

JOURNAL_MAX_USE="${JOURNAL_MAX_USE:-300M}"
JOURNAL_MAX_FILE_SIZE="${JOURNAL_MAX_FILE_SIZE:-50M}"
SSHD_PORT="${SSHD_PORT:-ssh}"
SSHD_MAXRETRY="${SSHD_MAXRETRY:-5}"
SSHD_FINDTIME="${SSHD_FINDTIME:-600}"
SSHD_BANTIME="${SSHD_BANTIME:-3600}"
SKIP_JOURNAL="${SKIP_JOURNAL:-0}"
SKIP_APT="${SKIP_APT:-0}"
SKIP_FAIL2BAN="${SKIP_FAIL2BAN:-0}"
FORCE_JAIL_CONFIG="${FORCE_JAIL_CONFIG:-0}"
JAIL_MARKER="# Managed by server-maintenance.sh — edit freely, or delete this line to stop future runs from touching this file"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$1"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "This script needs root. Run it with: sudo $0"
  exit 1
fi

log "Disk usage before"
df -h /

# --------------------------- systemd-journald -------------------------
if [ "$SKIP_JOURNAL" != "1" ] && command -v journalctl >/dev/null 2>&1; then
  log "Capping systemd-journald (SystemMaxUse=$JOURNAL_MAX_USE, SystemMaxFileSize=$JOURNAL_MAX_FILE_SIZE)"
  JCONF=/etc/systemd/journald.conf

  set_journald_key() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" "$JCONF"; then
      sed -i "s/^${key}=.*/${key}=${value}/" "$JCONF"
    elif grep -qE "^#${key}=" "$JCONF"; then
      sed -i "s/^#${key}=.*/${key}=${value}/" "$JCONF"
    else
      sed -i "/^\[Journal\]/a ${key}=${value}" "$JCONF"
    fi
  }

  set_journald_key "SystemMaxUse" "$JOURNAL_MAX_USE"
  set_journald_key "SystemMaxFileSize" "$JOURNAL_MAX_FILE_SIZE"

  systemctl restart systemd-journald
  journalctl --vacuum-size="$JOURNAL_MAX_USE"
else
  warn "Skipping journald step"
fi

# ------------------------------- apt cleanup ----------------------------
if [ "$SKIP_APT" != "1" ] && command -v apt-get >/dev/null 2>&1; then
  log "Cleaning up apt (autoremove + autoclean)"
  apt-get autoremove --purge -y
  apt-get autoclean -y
else
  warn "Skipping apt cleanup step (not skipped by flag, or apt-get not found)"
fi

# ------------------------------- fail2ban -------------------------------
if [ "$SKIP_FAIL2BAN" != "1" ] && command -v apt-get >/dev/null 2>&1; then
  if ! command -v fail2ban-client >/dev/null 2>&1; then
    log "Installing fail2ban"
    apt-get install -y fail2ban
  else
    log "fail2ban already installed"
  fi

  JAIL_LOCAL=/etc/fail2ban/jail.local
  if [ -f "$JAIL_LOCAL" ] && ! grep -qF "$JAIL_MARKER" "$JAIL_LOCAL" && [ "$FORCE_JAIL_CONFIG" != "1" ]; then
    warn "$JAIL_LOCAL already exists and isn't managed by this script — leaving it alone (set FORCE_JAIL_CONFIG=1 to overwrite)"
  else
    log "Writing sshd jail config to $JAIL_LOCAL"
    cat > "$JAIL_LOCAL" <<EOF
$JAIL_MARKER
[sshd]
enabled = true
port = $SSHD_PORT
filter = sshd
logpath = /var/log/auth.log
maxretry = $SSHD_MAXRETRY
findtime = $SSHD_FINDTIME
bantime = $SSHD_BANTIME
bantime.increment = true
EOF
  fi

  systemctl enable fail2ban
  systemctl restart fail2ban

  log "fail2ban sshd jail status"
  # systemctl restart returns as soon as the process starts, but the
  # control socket takes a moment to come up — retry briefly instead
  # of racing it.
  jail_status_ok=0
  for _ in 1 2 3 4 5; do
    if fail2ban-client status sshd 2>/dev/null; then
      jail_status_ok=1
      break
    fi
    sleep 1
  done
  [ "$jail_status_ok" -eq 1 ] || warn "fail2ban-client status sshd never came up after restart — check manually"
else
  warn "Skipping fail2ban step"
fi

log "Disk usage after"
df -h /

log "Maintenance complete ✔"
