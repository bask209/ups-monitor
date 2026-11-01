#!/bin/sh
# NUT driver + upsd for the USB UPS. Container root == host user (rootless).
#
# Both run in the foreground as children of this script so that:
#  - the driver's own log messages go to the container log (journald) instead of
#    a syslog that does not exist in the container, and
#  - if EITHER process dies the container exits and systemd restarts it. With
#    `upsdrvctl start` the driver daemonized and a dead driver left upsd alive
#    but serving stale data.
set -e
mkdir -p /var/state/ups

/usr/lib/nut/usbhid-ups -a ups -u root -F &
DRV=$!
/usr/sbin/upsd -F -u root &
UPSD=$!

trap 'kill $DRV $UPSD 2>/dev/null; wait; exit 0' TERM INT

while kill -0 "$DRV" 2>/dev/null && kill -0 "$UPSD" 2>/dev/null; do
    sleep 1
done

echo "entrypoint: driver (pid $DRV) or upsd (pid $UPSD) exited - stopping container so it is restarted" >&2
kill "$DRV" "$UPSD" 2>/dev/null || true
exit 1
